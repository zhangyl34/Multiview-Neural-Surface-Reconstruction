import torch
import torch.nn as nn
import numpy as np

from utils import rend_util
from model.embedder import *
from model.ray_tracing import RayTracing
from model.sample_network import SampleNetwork

class ImplicitNetwork(nn.Module):
    def __init__(self,
                feature_vector_size,
                d_in,
                d_out,
                dims,
                geometric_init=True,
                bias=1.0,
                skip_in=(),
                weight_norm=True,
                multires=0
                ):
        super().__init__()
        # [3 , 512,512,512,512,512,512,512,512 , 1+256]
        dims = [d_in] + dims + [d_out + feature_vector_size]

        self.embed_fn = None
        # 6
        if multires > 0:
            embed_fn, input_ch = get_embedder(multires)
            self.embed_fn = embed_fn
            # 39
            dims[0] = input_ch

        # 10
        self.num_layers = len(dims)
        # [4]
        self.skip_in = skip_in
        # range(0,9)
        for l in range(0, self.num_layers - 1):
            if l + 1 in self.skip_in:
                out_dim = dims[l + 1] - dims[0]
            else:
                out_dim = dims[l + 1]
            lin = nn.Linear(dims[l], out_dim)
            # True 初始化 lin 的参数
            if geometric_init:
                if l == self.num_layers - 2:
                    torch.nn.init.normal_(lin.weight, mean=np.sqrt(np.pi) / np.sqrt(dims[l]), std=0.0001)
                    torch.nn.init.constant_(lin.bias, -bias)
                elif multires > 0 and l == 0:
                    torch.nn.init.constant_(lin.bias, 0.0)
                    torch.nn.init.constant_(lin.weight[:, 3:], 0.0)
                    torch.nn.init.normal_(lin.weight[:, :3], 0.0, np.sqrt(2) / np.sqrt(out_dim))
                elif multires > 0 and l in self.skip_in:
                    torch.nn.init.constant_(lin.bias, 0.0)
                    torch.nn.init.normal_(lin.weight, 0.0, np.sqrt(2) / np.sqrt(out_dim))
                    torch.nn.init.constant_(lin.weight[:, -(dims[0] - 3):], 0.0)
                else:
                    torch.nn.init.constant_(lin.bias, 0.0)
                    torch.nn.init.normal_(lin.weight, 0.0, np.sqrt(2) / np.sqrt(out_dim))
            # True
            if weight_norm:
                lin = nn.utils.weight_norm(lin)
            # 设置 linl 为 lin
            setattr(self, "lin" + str(l), lin)

        self.softplus = nn.Softplus(beta=100)

    def forward(self, input, compute_grad=False):
        # 将 x 拓展为 [x,sin(x),cos(x),...,sin(32x),cos(32x)] 3x13 维
        if self.embed_fn is not None:
            input = self.embed_fn(input)
        x = input
        # range(0,9)
        for l in range(0, self.num_layers - 1):
            lin = getattr(self, "lin" + str(l))
            # [4]
            if l in self.skip_in:
                x = torch.cat([x, input], 1) / np.sqrt(2)
            x = lin(x)
            # < 8
            if l < self.num_layers - 2:
                x = self.softplus(x)
        return x

    def gradient(self, x):
        ''' 利用 autograd 获取 f 对 x 的梯度'''
        x.requires_grad_(True)
        y = self.forward(x)[:,:1]
        d_output = torch.ones_like(y, requires_grad=False, device=y.device)
        gradients = torch.autograd.grad(
            outputs=y,
            inputs=x,
            grad_outputs=d_output,
            create_graph=True,
            retain_graph=True,
            only_inputs=True)[0]
        return gradients.unsqueeze(1)

class RenderingNetwork(nn.Module):
    def __init__(self,
                feature_vector_size,
                mode,
                d_in,
                d_out,
                dims,
                weight_norm=True,
                multires_view=0
                ):
        super().__init__()
        # idr
        self.mode = mode
        # [9+256 , 512,512,512,512 , 3]
        dims = [d_in + feature_vector_size] + dims + [d_out]

        self.embedview_fn = None
        # 4
        if multires_view > 0:
            embedview_fn, input_ch = get_embedder(multires_view)
            self.embedview_fn = embedview_fn
            # (27-3)
            dims[0] += (input_ch - 3)

        # 6
        self.num_layers = len(dims)
        # range(0,5)
        for l in range(0, self.num_layers - 1):
            out_dim = dims[l + 1]
            lin = nn.Linear(dims[l], out_dim)
            # True
            if weight_norm:
                lin = nn.utils.weight_norm(lin)
            # 设置 linl 为 lin
            setattr(self, "lin" + str(l), lin)

        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()

    def forward(self, points, normals, view_dirs, feature_vectors):
        if self.embedview_fn is not None:
            # 将 view_dirs 拓展为 [x,sin(x),cos(x),...,sin(8x),cos(8x)] 3x9 维
            view_dirs = self.embedview_fn(view_dirs)
        if self.mode == 'idr':
            # [3 + 27 + 3 + 256]
            rendering_input = torch.cat([points, view_dirs, normals, feature_vectors], dim=-1)
        elif self.mode == 'no_view_dir':
            rendering_input = torch.cat([points, normals, feature_vectors], dim=-1)
        elif self.mode == 'no_normal':
            rendering_input = torch.cat([points, view_dirs, feature_vectors], dim=-1)
        x = rendering_input
        # range(0,5)
        for l in range(0, self.num_layers - 1):
            lin = getattr(self, "lin" + str(l))
            x = lin(x)
            # < 4
            if l < self.num_layers - 2:
                x = self.relu(x)
        x = self.tanh(x)
        return x

class IDRNetwork(nn.Module):
    def __init__(self, conf):
        super().__init__()
        # 256
        self.feature_vector_size = conf.get_int('feature_vector_size')
        # 1.0
        self.object_bounding_sphere = conf.get_float('ray_tracer.object_bounding_sphere')
        self.implicit_network = ImplicitNetwork(self.feature_vector_size, **conf.get_config('implicit_network'))
        self.rendering_network = RenderingNetwork(self.feature_vector_size, **conf.get_config('rendering_network'))
        self.ray_tracer = RayTracing(**conf.get_config('ray_tracer'))
        self.sample_network = SampleNetwork()

    def forward(self, input):
        # Parse model input
        intrinsics = input["intrinsics"]
        uv = input["uv"]
        pose = input["pose"]
        object_mask = input["object_mask"].reshape(-1)
        # 获取 v, c
        ray_dirs, cam_loc = rend_util.get_camera_params(uv, pose, intrinsics)
        # 1,10000
        batch_size, num_pixels, _ = ray_dirs.shape

        self.implicit_network.eval()
        with torch.no_grad():
            # 用 implicit_network 估计 sdf，进而寻找 rays 与 model 的交点
            # rays 与 model 的交点；交点 mask；t
            points, network_object_mask, dists = self.ray_tracer(sdf=lambda x: self.implicit_network(x)[:, 0],
                                                                 cam_loc=cam_loc,
                                                                 object_mask=object_mask,
                                                                 ray_directions=ray_dirs)
        self.implicit_network.train()

        points = (cam_loc.unsqueeze(1) + dists.reshape(batch_size, num_pixels, 1) * ray_dirs).reshape(-1, 3)
        sdf_output = self.implicit_network(points)[:, 0:1]
        ray_dirs = ray_dirs.reshape(-1, 3)
        if self.training:
            # 与 model 相交，且 network 也认为相交
            surface_mask = network_object_mask & object_mask
            surface_points = points[surface_mask]
            surface_dists = dists[surface_mask].unsqueeze(-1)
            surface_ray_dirs = ray_dirs[surface_mask]
            surface_cam_loc = cam_loc.unsqueeze(1).repeat(1, num_pixels, 1).reshape(-1, 3)[surface_mask]
            surface_output = sdf_output[surface_mask]  # [:,1]
            N = surface_points.shape[0]

            # eikonal loss
            eik_bounding_box = self.object_bounding_sphere  # 1.0
            n_eik_points = batch_size * num_pixels // 2  # 5000
            # [5000,3] 在 -1～1 的范围内随机采样
            eikonal_points = torch.empty(n_eik_points, 3).uniform_(-eik_bounding_box, eik_bounding_box).cuda()
            eikonal_pixel_points = points.clone()
            eikonal_pixel_points = eikonal_pixel_points.detach()
            # [10000,3]
            eikonal_points = torch.cat([eikonal_points, eikonal_pixel_points], 0)

            points_all = torch.cat([surface_points, eikonal_points], dim=0)
            output = self.implicit_network(surface_points)
            surface_sdf_values = output[:N, 0:1].detach()
            g = self.implicit_network.gradient(points_all)
            # 只取第一行 sdf 对 xyz 的偏导
            surface_points_grad = g[:N, 0, :].clone().detach()
            grad_theta = g[N:, 0, :]
            differentiable_surface_points = self.sample_network(surface_output,
                                                                surface_sdf_values,
                                                                surface_points_grad,
                                                                surface_dists,
                                                                surface_cam_loc,
                                                                surface_ray_dirs)

        else:
            surface_mask = network_object_mask
            differentiable_surface_points = points[surface_mask]
            grad_theta = None

        view = -ray_dirs[surface_mask]

        rgb_values = torch.ones_like(points).float().cuda()
        if differentiable_surface_points.shape[0] > 0:
            rgb_values[surface_mask] = self.get_rbg_value(differentiable_surface_points, view)

        output = {
            'points': points,
            'rgb_values': rgb_values,
            'sdf_output': sdf_output,
            'network_object_mask': network_object_mask,
            'object_mask': object_mask,
            'grad_theta': grad_theta
        }

        return output

    def get_rbg_value(self, points, view_dirs):
        output = self.implicit_network(points)
        g = self.implicit_network.gradient(points)
        normals = g[:, 0, :]

        feature_vectors = output[:, 1:]
        rgb_vals = self.rendering_network(points, normals, view_dirs, feature_vectors)

        return rgb_vals
