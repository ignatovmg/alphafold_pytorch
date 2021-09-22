import torch
from torch import nn
import torch.functional as F
import math
import functools

import modules
import structure
#import r3
import quat_affine
#import all_atom


class DockerIteration(nn.Module):
    def __init__(self, config, global_config):
        super().__init__()
        self.InputEmbedder = modules.InputEmbedder(config['InputEmbedder'], global_config)
        self.Evoformer = nn.Sequential(*[modules.EvoformerIteration(config['Evoformer']['EvoformerIteration'], global_config) for x in range(config['Evoformer']['num_iter'])])
        self.EvoformerExtractSingleLig = nn.Linear(global_config['rep_1d']['num_c'], global_config['num_single_c'])
        self.EvoformerExtractSingleRec = nn.Linear(global_config['rep_1d']['num_c'], global_config['num_single_c'])
        self.StructureModule = structure.StructureModule(config['StructureModule'], global_config)
        self.PredictLDDT = None

        self.config = config
        self.global_config = global_config

    def forward(self, input):
        x = self.InputEmbedder(input)
        x = self.Evoformer(x)
        pair = x['pair']
        rec_single = self.EvoformerExtractSingleRec(x['r1d'])
        lig_single = self.EvoformerExtractSingleLig(x['l1d'][:, 0])
        struct_out = self.StructureModule({'r1d': rec_single, 'l1d': lig_single, 'pair': pair})
        return struct_out

        assert struct_out['rec_T'].shape[0] == 1
        final_all_atom = make_all_atom(struct_out['rec_T'][-1], struct_out['rec_torsions'][-1], input['target']['rec_aatype'])

        ret = {}
        traj_scale = torch.tensor([1.] * 4 + [self.global_config['position_scale']] * 3, device=struct_out['rec_T'].device, dtype=struct_out['rec_T'].dtype)
        ret['rec_traj'] = struct_out['rec_T_inter'] * traj_scale[None, :, None]
        ret['lig_traj'] = struct_out['lig_T_inter'] * traj_scale[None, :, None]
        ret['sidechains'] = final_all_atom

        atom14_pred_positions = r3.vecs_to_tensor(final_all_atom['atom_pos'])
        ret['final_atom14_positions'] = atom14_pred_positions  # (N, 14, 3)
        ret['final_atom14_mask'] = input['atom14_atom_exists']  # (N, 14)

        atom37_pred_positions = all_atom.atom14_to_atom37(atom14_pred_positions, input)
        atom37_pred_positions *= input['atom37_atom_exists'][:, :, None]
        ret['final_atom_positions'] = atom37_pred_positions  # (N, 37, 3)
        ret['final_atom_mask'] = input['atom37_atom_exists']  # (N, 37)
        ret['final_rec_affines'] = ret['rec_traj'][..., -1]
        return ret


def l2_normalize(x, axis=-1, epsilon=1e-12):
    return x / torch.sqrt(torch.maximum(torch.sum(x**2, dim=axis, keepdim=True), epsilon))


def make_all_atom(
        rec_T: torch.Tensor,  # (N, 7)
        rec_torsions_unnorm: torch.Tensor,  # (N, 14)
        aatype: torch.Tensor  # (N)
):
    rec_T = quat_affine.QuatAffine(rec_T[:, :4], torch.tensor_split(rec_T[:, 4:], 3, dim=-1))
    backb_to_global = r3.rigids_from_quataffine(rec_T)

    rec_torsions_unnorm = rec_torsions_unnorm.view(rec_torsions_unnorm.shape[0], 7, 2)
    rec_torsions = l2_normalize(rec_torsions_unnorm)

    outputs = {
        'angles_sin_cos': rec_torsions,  # (N, 7, 2)
        'unnormalized_angles_sin_cos': rec_torsions_unnorm,  # (N, 7, 2)
    }

    all_frames_to_global = all_atom.torsion_angles_to_frames(
        aatype,
        backb_to_global,
        rec_torsions
    )

    pred_positions = all_atom.frames_and_literature_positions_to_atom14_pos(aatype, all_frames_to_global)

    outputs.update({
        'atom_pos': pred_positions,  # r3.Vecs (N, 14)
        'frames': all_frames_to_global,  # r3.Rigids (N, 8)
    })
    return outputs


def backbone_loss(ret, batch, value, config):
    """Backbone FAPE Loss.

    Jumper et al. (2021) Suppl. Alg. 20 "StructureModule" line 17

    Args:
      ret: Dictionary to write outputs into, needs to contain 'loss'.
      batch: Batch, needs to contain 'backbone_affine_tensor',
        'backbone_affine_mask'.
      value: Dictionary containing structure module output, needs to contain
        'traj', a trajectory of rigids.
      config: Configuration of loss, should contain 'fape.clamp_distance' and
        'fape.loss_unit_distance'.
    """
    rec_affine_trajectory = quat_affine.QuatAffine.from_tensor(value['rec_traj'])  # (num_traj, N, 7)
    rec_rigid_trajectory = r3.rigids_from_quataffine(rec_affine_trajectory)
    rec_gt_affine = quat_affine.QuatAffine.from_tensor(batch['backbone_affine_tensor'])
    rec_gt_rigid = r3.rigids_from_quataffine(rec_gt_affine)
    backbone_mask = batch['backbone_affine_mask']

    lig_trajectory_vecs = r3.vecs_from_tensor(value['lig_traj'][:, :, -3:, :])
    lig_gt_vecs = r3.vecs_from_tensor(batch['lig_gt_coords'])
    lig_mask = batch['lig_mask']

    fape_loss_fn = functools.partial(
        all_atom.frame_aligned_point_error,
        l1_clamp_distance=config['fape_clamp_distance'],
        length_scale=config['fape_loss_unit_distance']
    )

    fape_loss = config['rec_fape_weight'] * fape_loss_fn(
        rec_rigid_trajectory,
        rec_gt_rigid,
        backbone_mask,
        rec_rigid_trajectory.trans,
        rec_gt_rigid.trans,
        backbone_mask
    )

    fape_loss += config['lig_fape_weight'] * fape_loss_fn(
        rec_rigid_trajectory,
        rec_gt_rigid,
        backbone_mask,
        lig_trajectory_vecs,
        lig_gt_vecs,
        lig_mask)

    #if 'use_clamped_fape' in batch:
    if False:
        # Jumper et al. (2021) Suppl. Sec. 1.11.5 "Loss clamping details"
        use_clamped_fape = torch.as_tensor(batch['use_clamped_fape'], torch.float32)
        unclamped_fape_loss_fn = functools.partial(
            all_atom.frame_aligned_point_error,
            l1_clamp_distance=None,
            length_scale=config.fape.loss_unit_distance
        )
        unclamped_fape_loss_fn = jax.vmap(unclamped_fape_loss_fn, (0, None, None, 0, None, None))
        fape_loss_unclamped = unclamped_fape_loss_fn(rigid_trajectory, gt_rigid,
                                                     backbone_mask,
                                                     rigid_trajectory.trans,
                                                     gt_rigid.trans,
                                                     backbone_mask)

        fape_loss = (fape_loss * use_clamped_fape + fape_loss_unclamped * (1 - use_clamped_fape))

    ret['fape'] = fape_loss[-1]
    ret['loss'] += jnp.mean(fape_loss)


def example3():
    from config import config, DATA_DIR
    with torch.no_grad():
        model = DockerIteration(config, config).cuda()

        pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print('Num params:', pytorch_total_params)

        from dataset import DockingDataset
        ds = DockingDataset(DATA_DIR, 'train_split/debug.json')
        #print(ds[0])
        item = ds[0]

        for k1, v1 in item.items():
            print(k1)
            for k2, v2 in v1.items():
                v1[k2] = torch.as_tensor(v2)[None].cuda()
                print('    ', k2, v1[k2].shape, v1[k2].dtype)

        print(item['fragments']['num_res'])
        print(item['fragments']['num_atoms'])
        #model(item)
        print({k: v.shape for k, v in model(item).items()})


if __name__ == '__main__':
    example3()