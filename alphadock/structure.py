import torch
from torch import nn
import torch.functional as F
from torch.utils.checkpoint import checkpoint
import math

from alphadock import quat_affine


class InvariantPointAttention(torch.nn.Module):
    def __init__(self, config, global_config):
        super().__init__()

        self.num_head = config['num_head']
        self.num_scalar_qk = config['num_scalar_qk']
        self.num_point_qk = config['num_point_qk']
        self.num_2d_qk = config['num_2d_qk']
        self.num_scalar_v = config['num_scalar_v']
        self.num_point_v = config['num_point_v']
        self.num_2d_v = config['num_2d_v']

        self.num_output_c = global_config['num_single_c'] #config['num_channel']
        self.rep_1d_num_c = global_config['num_single_c']
        self.rep_2d_num_c = global_config['rep_2d']['num_c']

        self.rec_kqv_1d = nn.Linear(self.rep_1d_num_c, (self.num_scalar_qk * 2 + self.num_scalar_v) * self.num_head, bias=False)
        self.rec_kqv_point = nn.Linear(self.rep_1d_num_c, (self.num_point_qk * 2 + self.num_point_v) * self.num_head * 3, bias=False)

        self.lig_kqv_1d = nn.Linear(self.rep_1d_num_c, (self.num_scalar_qk * 2 + self.num_scalar_v) * self.num_head, bias=False)
        self.lig_kqv_point = nn.Linear(self.rep_1d_num_c, (self.num_point_qk * 2 + self.num_point_v) * self.num_head * 3, bias=False)

        self.rr_kqv_2d = nn.Linear(self.rep_2d_num_c, self.num_head, bias=False)
        self.ll_kqv_2d = nn.Linear(self.rep_2d_num_c, self.num_head, bias=False)
        self.rl_kqv_2d = nn.Linear(self.rep_2d_num_c, self.num_head, bias=False)
        self.lr_kqv_2d = nn.Linear(self.rep_2d_num_c, self.num_head, bias=False)

        self.final = nn.Linear(self.num_head * (self.rep_2d_num_c + self.num_scalar_v + 4 * self.num_point_v), self.num_output_c)

    def forward(self, rec_1d, lig_1d, rep_2d, rec_T, lig_T):
        batch = rec_1d.shape[0]
        num_res = rec_1d.shape[1]
        num_atoms = lig_1d.shape[1]

        rec_T = quat_affine.QuatAffine.from_tensor(rec_T)
        lig_T = quat_affine.QuatAffine.from_tensor(lig_T)

        kqv = self.rec_kqv_1d(rec_1d).view([batch, num_res, self.num_head, self.num_scalar_qk * 2 + self.num_scalar_v])
        rec_k_1d, rec_q_1d, rec_v_1d = torch.split(kqv, [self.num_scalar_qk, self.num_scalar_qk, self.num_scalar_v], dim=-1)

        kqv = self.lig_kqv_1d(lig_1d).view(batch, num_atoms, self.num_head, self.num_scalar_qk * 2 + self.num_scalar_v)
        lig_k_1d, lig_q_1d, lig_v_1d = torch.split(kqv, [self.num_scalar_qk, self.num_scalar_qk, self.num_scalar_v], dim=-1)  # [b, a, h, c]

        rr = self.rr_kqv_2d(rep_2d[:, :num_res, :num_res]).view([batch, num_res, num_res, self.num_head])
        ll = self.ll_kqv_2d(rep_2d[:, num_res:, num_res:]).view([batch, num_atoms, num_atoms, self.num_head])
        rl = self.rl_kqv_2d(rep_2d[:, :num_res, num_res:]).view([batch, num_res, num_atoms, self.num_head])
        lr = self.lr_kqv_2d(rep_2d[:, num_res:, :num_res]).view([batch, num_atoms, num_res, self.num_head])

        Wc = math.sqrt(2. / (9. * self.num_point_qk))
        Wl = math.sqrt(1. / 3.)

        kqv_local = self.rec_kqv_point(rec_1d)  # [b, r, c]
        kqv_global = torch.stack(rec_T.apply_to_point(torch.chunk(kqv_local, 3, dim=-1)))  # [3, b, r, x]
        kqv_global = kqv_global.movedim(0, 2)   # [b, r, 3, x]
        kqv_global = kqv_global.view(*kqv_global.shape[:-1], self.num_head, self.num_point_qk*2+self.num_point_v)  # [b, r, 3, h, x]
        rec_k_point, rec_q_point, rec_v_point = torch.split(kqv_global, [self.num_point_qk, self.num_point_qk, self.num_point_v], dim=-1)

        kqv_local = self.lig_kqv_point(lig_1d)  # [b, r, c]
        kqv_global = torch.stack(lig_T.apply_to_point(torch.chunk(kqv_local, 3, dim=-1)))  # [3, b, r, x]
        kqv_global = kqv_global.movedim(0, 2)   # [b, r, 3, x]
        kqv_global = kqv_global.view(*kqv_global.shape[:-1], self.num_head, self.num_point_qk*2+self.num_point_v)  # [b, r, 3, h, x]
        lig_k_point, lig_q_point, lig_v_point = torch.split(kqv_global, [self.num_point_qk, self.num_point_qk, self.num_point_v], dim=-1)

        q_point = torch.cat([rec_q_point, lig_q_point], dim=1)  # [b, r, 3, h, p]
        k_point = torch.cat([rec_k_point, lig_k_point], dim=1)
        d2mat = torch.sum(torch.square(q_point[:, :, None] - k_point[:, None, :]), dim=3)  # -> [b, r+a, r+a, h, p]

        # add sq distances
        #aff = -self.d2_weights * Wc * d2mat.sum(axis=-2)
        aff = -Wc * d2mat.sum(axis=-1)   # TODO: add learnable weights

        # add pair bias
        aff[:, :num_res, :num_res] += rr
        aff[:, num_res:, num_res:] += ll
        aff[:, :num_res, num_res:] += rl
        aff[:, num_res:, :num_res] += lr

        # add 1d affinity
        aff += (Wl / math.sqrt(self.num_scalar_qk)) * torch.einsum('bihc,bjhc->bijh', torch.cat([rec_q_1d, lig_q_1d], dim=1), torch.cat([rec_k_1d, lig_k_1d], dim=1))

        weights = torch.softmax(aff, dim=2)  # bijh

        out = []
        out.append((weights[..., None] * rep_2d[..., None, :]).sum(2).flatten(start_dim=-2))  # [b, r+a, c*h]

        v_1d = torch.cat([rec_v_1d, lig_v_1d], dim=1)
        out.append((weights.unsqueeze(-1) * v_1d.unsqueeze(2)).sum(2).flatten(start_dim=-2))  # [b, r+a, c*h]

        v_point = torch.cat([rec_v_point, lig_v_point], dim=1)
        out_global = torch.einsum('bijh,bjdhp->bidph', weights, v_point)
        #out_global = out_global.movedim(1, 0)

        rec_out_local = torch.cat(rec_T.invert_point(torch.chunk(out_global[:, :num_res], 3, dim=2)), dim=2)
        lig_out_local = torch.cat(lig_T.invert_point(torch.chunk(out_global[:, num_res:], 3, dim=2)), dim=2)  # [b, i, 3, p, h]
        out_local = torch.cat([rec_out_local, lig_out_local], dim=1).permute([0, 1, 3, 4, 2])  # [b, i, p, h, 3]

        # add local coords
        out.append(out_local.flatten(start_dim=2))  # [b, a+r, p*h*3]

        # add norm
        out.append(torch.sqrt(torch.square(out_local).sum(-1)).flatten(start_dim=2))  # [b, a+r, p*h]

        return self.final(torch.cat(out, dim=-1))


class PredictSidechains(torch.nn.Module):
    def __init__(self, config, global_config):
        super().__init__()
        num_in_c = global_config['num_single_c']
        num_c = config['num_c']
        self.num_torsions = global_config['num_torsions']

        self.s_cur = nn.Linear(num_in_c, num_c)
        self.s_ini = nn.Linear(num_in_c, num_c)

        self.res1 = nn.Sequential(
            nn.Linear(num_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_c)
        )

        self.res2 = nn.Sequential(
            nn.Linear(num_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_c)
        )

        self.final = nn.Sequential(
            nn.ReLU(),
            nn.Linear(num_c, self.num_torsions * 2)
        )

    def forward(self, s_cur, s_ini):
        a = self.s_cur(s_cur.clone()) + self.s_ini(s_ini)
        a += self.res1(a.clone())
        a += self.res2(a.clone())
        return self.final(a).reshape(*a.shape[:-1], self.num_torsions, 2)


class PredictLDDT(torch.nn.Module):
    def __init__(self, config, global_config):
        super().__init__()
        num_in_c = global_config['num_single_c']
        num_c = config['num_c']
        num_bins = config['num_bins']

        self.layers = nn.Sequential(
            nn.LayerNorm(num_in_c),
            nn.Linear(num_in_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_bins)
            #nn.Softmax(-1)
        )

    def forward(self, rep_1d):
        return self.layers(rep_1d)


class StructureModuleIteration(torch.nn.Module):
    def __init__(self, config, global_config):
        super().__init__()
        self.InvariantPointAttention = InvariantPointAttention(config['InvariantPointAttention'], global_config)
        self.drop = nn.Dropout(0.1)
        self.rec_norm = nn.LayerNorm(global_config['num_single_c'])
        self.lig_norm = nn.LayerNorm(global_config['num_single_c'])

        num_1dc = global_config['num_single_c']
        self.transition = nn.Sequential(
            nn.Linear(num_1dc, num_1dc),
            nn.ReLU(),
            nn.Linear(num_1dc, num_1dc),
            nn.ReLU(),
            nn.Linear(num_1dc, num_1dc)
        )

        self.backbone_update = nn.Linear(num_1dc, 6)
        self.lig_atoms_update = nn.Linear(num_1dc, 6)

        self.PredictSidechains = PredictSidechains(config['PredictSidechains'], global_config)
        self.PredictRecLDDT = PredictLDDT(config['PredictRecLDDT'], global_config)
        self.PredictLigLDDT = PredictLDDT(config['PredictLigLDDT'], global_config)
        #self.PredictRecLigDgram = nn.Linear(global_config['rep_2d']['num_c'], )

    def forward(self, inputs):
        rec_1d_init, rec_1d, lig_1d, rep_2d, rec_T, lig_T = inputs['rec_1d_init'], inputs['rec_1d'], inputs['lig_1d'], inputs['rep_2d'], inputs['rec_T'], inputs['lig_T']

        num_res = rec_1d.shape[1]
        num_atoms = lig_1d.shape[1]

        # IPA
        s_update = self.InvariantPointAttention(rec_1d.clone(), lig_1d.clone(), rep_2d, rec_T, lig_T)
        rec_1d += s_update[:, :num_res]
        lig_1d += s_update[:, num_res:]

        rec_1d = self.rec_norm(self.drop(rec_1d))
        lig_1d = self.lig_norm(self.drop(lig_1d))

        # transition
        s_update = self.transition(torch.cat([rec_1d, lig_1d], dim=1))
        rec_1d = self.drop(rec_1d + s_update[:, :num_res])
        lig_1d = self.drop(lig_1d + s_update[:, num_res:])

        # update backbone
        rec_T = quat_affine.QuatAffine.from_tensor(rec_T)
        lig_T = quat_affine.QuatAffine.from_tensor(lig_T)
        rec_T = rec_T.pre_compose(self.backbone_update(rec_1d.clone()))
        lig_T = lig_T.pre_compose(self.lig_atoms_update(lig_1d.clone()))

        # sidechains
        rec_torsions = inputs['rec_torsions'] + self.PredictSidechains(rec_1d, rec_1d_init)

        return {
            'rec_1d': rec_1d,
            'lig_1d': lig_1d,
            'rec_T': rec_T.to_tensor(),
            'lig_T': lig_T.to_tensor(),
            'rec_torsions': rec_torsions,  #  (1, Nres, 7, 2)
            'rec_lddt': self.PredictRecLDDT(rec_1d.clone()),
            'lig_lddt': self.PredictLigLDDT(lig_1d.clone())
        }


class PredictAffinity(torch.nn.Module):
    def __init__(self, config, global_config):
        super().__init__()
        num_in_c = global_config['num_single_c']
        num_c = config['num_c']
        num_bins = config['num_bins']

        self.layers = nn.Sequential(
            nn.LayerNorm(num_in_c),
            nn.Linear(num_in_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_c),
            nn.ReLU(),
            nn.Linear(num_c, num_bins)
            #nn.Softmax(-1)
        )

    def forward(self, rep_1d):
        return self.layers(rep_1d)


class StructureModule(torch.nn.Module):
    def __init__(self, config, global_config):
        super().__init__()
        self.num_iter = config['num_iter']
        num_1dc = global_config['num_single_c']

        self.layers = nn.ModuleList([StructureModuleIteration(config['StructureModuleIteration'], global_config) for _ in range(self.num_iter)])
        self.norm_rec_1d_init = nn.LayerNorm(num_1dc)
        self.norm_lig_1d_init = nn.LayerNorm(num_1dc)
        self.norm_2d_init = nn.LayerNorm(global_config['rep_2d']['num_c'])
        self.rec_1d_proj = nn.Linear(num_1dc, num_1dc)
        self.lig_1d_proj = nn.Linear(num_1dc, num_1dc)
        self.pred_affinity = PredictAffinity(config['PredictAffinity'], global_config)

        self.position_scale = global_config['position_scale']
        self.config = config

    def forward(self, inputs):
        # batch size must be one
        assert inputs['r1d'].shape[0] == 1

        rec_1d_init = self.norm_rec_1d_init(inputs['r1d'])
        lig_1d_init = self.norm_lig_1d_init(inputs['l1d'])
        pair = self.norm_2d_init(inputs['pair'])

        rec_1d = self.rec_1d_proj(rec_1d_init)
        lig_1d = self.lig_1d_proj(lig_1d_init)

        # Start with crystal rec positions
        rec_T = inputs['rec_bb_affine'].clone().to(rec_1d.device)

        # Set masked frames to origin
        rec_T_masked = torch.where(inputs['rec_bb_affine_mask'] < 1)[1]
        rec_T[:, rec_T_masked, :] = 0
        rec_T[:, rec_T_masked, 0] = 1
        rec_T[:, :, -3:] = rec_T[:, :, -3:] / self.position_scale
        rec_T.requires_grad = True

        # Set up ligand starting frames
        # TODO: maybe use COM of rec instead of zeros ?
        lig_T = torch.zeros((1, lig_1d.shape[1], 7), device=lig_1d.device, dtype=lig_1d.dtype)
        lig_T[:, :, 0] = 1
        lig_T.requires_grad = True

        x = {
            'rec_1d_init': rec_1d_init,
            'rec_1d': rec_1d,
            'lig_1d': lig_1d,
            'rep_2d': pair,
            'rec_T': rec_T,
            'lig_T': lig_T,
            'rec_torsions': inputs['rec_torsions']
        }

        rec_T_inter = []
        rec_torsions_inter = []
        lig_T_inter = []
        rec_lddt = []
        lig_lddt = []

        #print({k: v.requires_grad for k, v in x.items()})

        def checkpoint_fun(function):
            return lambda input: function(input)

        for l in self.layers:
            if self.config['StructureModuleIteration']['checkpoint']:
                update = checkpoint(checkpoint_fun(l), x)
            else:
                update = l(x)
            x.update(update)
            rec_T_inter.append(x['rec_T'])
            lig_T_inter.append(x['lig_T'])
            rec_torsions_inter.append(x['rec_torsions'])
            rec_lddt.append(x['rec_lddt'])
            lig_lddt.append(x['lig_lddt'])

        aff_rep = self.pred_affinity(x['lig_1d'])
        affinities = []
        for start, end in zip(inputs['lig_starts'][0], inputs['lig_ends'][0]):
            affinities.append(aff_rep[:, start:end].mean(-2))

        return {
            'rec_T': torch.stack(rec_T_inter, dim=1),
            'lig_T': torch.stack(lig_T_inter, dim=1),
            'rec_torsions': torch.stack(rec_torsions_inter, dim=1),
            'lig_1d': x['lig_1d'],
            'rec_1d': x['rec_1d'],
            'rec_lddt': torch.stack(rec_lddt, dim=1),
            'lig_lddt': torch.stack(lig_lddt, dim=1),
            'lig_affinity': torch.stack(affinities, dim=1)  # (1, num_ligs, num_labels)
        }


def example():
    from config import config
    loc_config = config['StructureModule']['StructureModuleIteration']['InvariantPointAttention']
    model = InvariantPointAttention(loc_config, config)

    num_res = 10
    num_atoms = 5
    rec_1d = torch.ones((1, num_res, config['rep_1d']['num_c']))
    lig_1d = torch.ones((1, num_atoms, config['rep_1d']['num_c']))
    rep_2d = torch.ones((1, num_res+num_atoms, num_res+num_atoms, config['rep_2d']['num_c']))

    rot = torch.tile(torch.eye(3), (num_res, 1, 1)).movedim(0, -1)
    quat = quat_affine.rot_to_quat(rot)
    #print(quat)
    tr = [torch.full([num_res], 3), torch.full([num_res], 4), torch.full([num_res], 5)]
    #rec_T = quat_affine.make_transform_from_reference(torch.ones())
    rec_T = quat_affine.QuatAffine(quat, tr)

    rot = torch.tile(torch.eye(3), (num_atoms, 1, 1)).movedim(0, -1)
    quat = quat_affine.rot_to_quat(rot)
    tr = [torch.full([num_atoms], 3), torch.full([num_atoms], 4), torch.full([num_atoms], 5)]
    lig_T = quat_affine.QuatAffine(quat, tr)

    #print(lig_T.rotation[0][0])

    input = torch.chunk(torch.zeros((4, 3, 3, 6)), 3, dim=-2)
    #print(input[0].shape)
    #print(lig_T.apply_to_point(input, extra_dims=0)[0].shape)



    model(rec_1d, lig_1d, rep_2d, rec_T, lig_T)


if __name__ == '__main__':
    example()

