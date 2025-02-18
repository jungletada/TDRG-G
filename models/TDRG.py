import torch
import torch.nn as nn
import torch.nn.functional as F
from trans_utils.position_encoding import build_position_encoding
from trans_utils.transformer import build_transformer


class TopKMaxPooling(nn.Module):
    """
        Top-K Maxpooling
        Input: B x C x H x W
        Return: B x C
    """

    def __init__(self, kmax=1.0):
        super(TopKMaxPooling, self).__init__()
        self.kmax = kmax

    @staticmethod
    def get_positive_k(k, n):
        if k <= 0:
            return 0
        elif k < 1:
            return round(k * n)
        elif k > n:
            return int(n)
        else:
            return int(k)

    def forward(self, input):
        batch_size = input.size(0)
        num_channels = input.size(1)
        h = input.size(2)
        w = input.size(3)
        n = h * w  # number of regions
        kmax = self.get_positive_k(self.kmax, n)
        sorted, indices = torch.sort(input.view(batch_size, num_channels, n), dim=2, descending=True)
        region_max = sorted.narrow(2, 0, kmax)
        output = region_max.sum(2).div_(kmax)
        return output.view(batch_size, num_channels)

    def __repr__(self):
        return self.__class__.__name__ + ' (kmax=' + str(self.kmax) + ')'


class GraphConvolution(nn.Module):
    def __init__(self, in_dim, out_dim):
        super(GraphConvolution, self).__init__()
        self.relu = nn.LeakyReLU(0.2)
        self.weight = nn.Conv1d(in_dim, out_dim, 1)

    def forward(self, adj, nodes):
        nodes = torch.matmul(nodes, adj)
        nodes = self.relu(nodes)
        nodes = self.weight(nodes)
        nodes = self.relu(nodes)
        return nodes


class TDRG(nn.Module):
    def __init__(self, model, num_classes):
        super(TDRG, self).__init__()
        # backbone
        self.layer1 = nn.Sequential(
            model.conv1,
            model.bn1,
            model.relu,
            model.maxpool,
            model.layer1)
        self.layer2 = model.layer2
        self.layer3 = model.layer3
        self.layer4 = model.layer4
        self.backbone = nn.ModuleList([self.layer1, self.layer2, self.layer3, self.layer4])

        # hyper-parameters
        self.num_classes = num_classes
        self.in_planes = 2048
        self.transformer_dim = 512
        self.gcn_dim = 512
        self.num_queries = 1
        self.n_head = 4
        self.num_encoder_layers = 3
        self.num_decoder_layers = 0

        # transformer
        self.transform_7 = nn.Conv2d(self.in_planes, self.transformer_dim, 3, stride=2)
        self.transform_14 = nn.Conv2d(self.in_planes, self.transformer_dim, 1)
        self.transform_28 = nn.Conv2d(self.in_planes // 2, self.transformer_dim, 1)

        self.query_embed = nn.Embedding(self.num_queries, self.transformer_dim)
        self.positional_embedding = build_position_encoding(hidden_dim=self.transformer_dim, mode='learned')
        self.transformer = build_transformer(
            d_model=self.transformer_dim,
            nhead=self.n_head,
            num_encoder_layers=self.num_encoder_layers,
            num_decoder_layers=self.num_decoder_layers)

        self.kmp = TopKMaxPooling(kmax=0.05)
        self.GMP = nn.AdaptiveMaxPool2d(1)
        self.GAP = nn.AdaptiveAvgPool2d(1)
        self.GAP1d = nn.AdaptiveAvgPool1d(1)

        self.trans_classifier = nn.Linear(self.transformer_dim * 3, self.num_classes)

        # GCN
        self.constraint_classifier = nn.Conv2d(self.in_planes, num_classes, (1, 1), bias=False)

        self.guidance_transform = nn.Conv1d(self.transformer_dim, self.transformer_dim, 1)
        self.guidance_conv = nn.Conv1d(self.transformer_dim * 3, self.transformer_dim * 3, 1)
        self.guidance_bn = nn.BatchNorm1d(self.transformer_dim * 3)
        self.relu = nn.LeakyReLU(0.2)
        self.gcn_dim_transform = nn.Conv2d(self.in_planes, self.gcn_dim, (1, 1))

        self.matrix_transform = nn.Conv1d(self.gcn_dim + self.transformer_dim * 4, self.num_classes, 1)

        self.forward_gcn = GraphConvolution(self.transformer_dim + self.gcn_dim,
                                            self.transformer_dim + self.gcn_dim)

        self.mask_mat = nn.Parameter(torch.eye(self.num_classes).float())
        self.gcn_classifier = nn.Conv1d(self.transformer_dim + self.gcn_dim, self.num_classes, 1)

    def forward_backbone(self, x):
        # 1. forward ReNet101 backbone
        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)
        return x2, x3, x4

    @staticmethod
    def cross_scale_attention(x3, x4, x5):
        h3, h4, h5 = x3.shape[2], x4.shape[2], x5.shape[2]
        h_max = max(h3, h4, h5)
        x3 = F.interpolate(x3, size=(h_max, h_max), mode='bilinear', align_corners=True)
        x4 = F.interpolate(x4, size=(h_max, h_max), mode='bilinear', align_corners=True)
        x5 = F.interpolate(x5, size=(h_max, h_max), mode='bilinear', align_corners=True)

        mul = x3 * x4 * x5
        x3 = x3 + mul
        x4 = x4 + mul
        x5 = x5 + mul

        x3 = F.interpolate(x3, size=(h3, h3), mode='bilinear', align_corners=True)
        x4 = F.interpolate(x4, size=(h4, h4), mode='bilinear', align_corners=True)
        x5 = F.interpolate(x5, size=(h5, h5), mode='bilinear', align_corners=True)
        return x3, x4, x5

    def forward_transformer(self, x3, x4):
        # 2. forward cross scale attention and transformer unit

        # linear transform to transformer-dim-> downsample
        x5 = self.transform_7(x4)  # Conv3x3,  ↓64, no padding
        x4 = self.transform_14(x4)  # Conv1x1   ↓32
        x3 = self.transform_28(x3)  # Conv1x1   ↓16 

        # forward cross scale attention 
        x3, x4, x5 = self.cross_scale_attention(x3, x4, x5)

        # transformer encoder
        mask3 = torch.zeros_like(x3[:, 0, :, :], dtype=torch.bool, device=x3.device)
        mask4 = torch.zeros_like(x4[:, 0, :, :], dtype=torch.bool, device=x4.device)
        mask5 = torch.zeros_like(x5[:, 0, :, :], dtype=torch.bool, device=x5.device)

        pos3 = self.positional_embedding(x3)
        pos4 = self.positional_embedding(x4)
        pos5 = self.positional_embedding(x5)

        # forward transformer unit 
        _, feat3 = self.transformer(x3, mask3, self.query_embed.weight, pos3)  # ↓16
        _, feat4 = self.transformer(x4, mask4, self.query_embed.weight, pos4)  # ↓32
        _, feat5 = self.transformer(x5, mask5, self.query_embed.weight, pos5)  # ↓64

        # f3 f4 f5: structural guidance -> B x C x N
        f3 = feat3.view(feat3.shape[0], feat3.shape[1], -1).detach()
        f4 = feat4.view(feat4.shape[0], feat4.shape[1], -1).detach()
        f5 = feat5.view(feat5.shape[0], feat5.shape[1], -1).detach()

        # AdaptiveMaxPool2d -> B x C
        feat3 = self.GMP(feat3).view(feat3.shape[0], -1)
        feat4 = self.GMP(feat4).view(feat4.shape[0], -1)
        feat5 = self.GMP(feat5).view(feat5.shape[0], -1)

        feat = torch.cat((feat3, feat4, feat5), dim=1)
        feat = self.trans_classifier(feat)  # Linear -> cls_logits

        return f3, f4, f5, feat

    def forward_constraint(self, x):
        # 3. classification constraint
        # constraint_classifier -> Conv1x1
        activations = self.constraint_classifier(x)
        # Top-K Max Pooling, K = 0.05
        out = self.kmp(activations)
        return out

    def build_nodes(self, x, f4):
        # build nodes for GCN
        mask = self.constraint_classifier(x)  # Conv1x1
        mask = mask.view(mask.size(0), mask.size(1), -1)
        mask = torch.sigmoid(mask)
        mask = mask.transpose(1, 2)  # B x N x Cls

        x = self.gcn_dim_transform(x)  # Conv1x1
        x = x.view(x.size(0), x.size(1), -1)  # B x Cg x N
        v_g = torch.matmul(x, mask)  # B x Cg x Cls

        v_t = torch.matmul(f4, mask)  # (B x Ct x N) (B x N x Cls)
        v_t = v_t.detach()  # B x Ct x Cls
        v_t = self.guidance_transform(v_t)  # Conv1d 1x1

        nodes = torch.cat((v_g, v_t), dim=1)  # B x (Cg+Ct) x Cls
        return nodes

    def build_joint_correlation_matrix(self, f3, f4, f5, x):
        """5. build joint correlation matrix"""
        # Adaptive Pooling1d -> B x Ct x 1
        f4 = self.GAP1d(f4)
        f3 = self.GAP1d(f3)
        f5 = self.GAP1d(f5)

        trans_guid = torch.cat((f3, f4, f5), dim=1)

        trans_guid = self.guidance_conv(trans_guid)  # Conv1d
        trans_guid = self.guidance_bn(trans_guid)    # batchnorm
        trans_guid = self.relu(trans_guid)           # activation

        trans_guid = trans_guid.expand(
            trans_guid.size(0), trans_guid.size(1), x.size(2))  # B x 3Ct x Cls
        x = torch.cat((trans_guid, x), dim=1)  # B x (Cg+4Ct) x Cls
        joint_correlation = self.matrix_transform(x)  # B x Cls x Cls
        joint_correlation = torch.sigmoid(joint_correlation)
        return joint_correlation

    def forward(self, x):
        # 1. forward resnet backbone
        _, x3, x4 = self.forward_backbone(x)  # _, ↓16, ↓32

        # 2. structural relation (B x Ct x N) and (B x Cls) as logits
        f3, f4, f5, out_trans = self.forward_transformer(x3, x4)

        # 3. semantic-aware constraints
        # Use the last output of backbone to proceed Top-K maxpooling and make predictions
        out_sac = self.forward_constraint(x4)

        # 4. graph nodes (Why f4?)
        V = self.build_nodes(x4, f4)  # B x (Cg+Ct) x Cls

        # 5. joint correlation (adj) -> B x Cls x Cls
        A_s = self.build_joint_correlation_matrix(f3, f4, f5, V)

        # 6.forward GCN  B x (Cg+Ct) x Cls
        G = self.forward_gcn(A_s, V) + V
        out_gcn = self.gcn_classifier(G)  # B x Cls x Cls

        # 7. get GCN cls_logits: mask_mat(identity of Cls)
        # Here diag_gcn is equal to out_gcn
        # diag_gcn = torch.diagonal(out_gcn, dim1=-2, dim2=-1)
        mask_mat = self.mask_mat.detach()
        out_gcn = (out_gcn * mask_mat).sum(-1)
        # return three output logits with B x C
        return out_trans, out_gcn, out_sac

    def get_config_optim(self, lr, lrp):
        small_lr_layers = list(map(id, self.backbone.parameters()))
        large_lr_layers = filter(lambda p: id(p) not in small_lr_layers, self.parameters())
        return [
            {'params': self.backbone.parameters(), 'lr': lr * lrp},
            {'params': large_lr_layers, 'lr': lr},
        ]


if __name__ == '__main__':
    import torchvision

    model_dict = {'TDRG': TDRG}
    res101 = torchvision.models.resnet101(pretrained=True)
    model = TDRG(res101, num_classes=11)

    img_input = torch.randn(2, 3, 224, 224)
    with torch.no_grad():
        out_trans, out_gcn, out_sac = model(img_input)
