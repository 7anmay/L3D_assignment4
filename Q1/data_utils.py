import os
import torch
import imageio
import numpy as np
import matplotlib.pyplot as plt

from PIL import Image
from plyfile import PlyData
from torch.utils.data import Dataset
from pytorch3d.renderer.cameras import PerspectiveCameras, look_at_view_transform

SH_C0 = 0.28209479177387814
SH_C1 = 0.4886025119029199
SH_C2_0 = 1.0925484305920792
SH_C2_1 = -1.0925484305920792
SH_C2_2 = 0.31539156525252005
SH_C2_3 = -1.0925484305920792
SH_C2_4 = 0.5462742152960396
SH_C3_0 = -0.5900435899266435
SH_C3_1 = 2.890611442640554
SH_C3_2 = -0.4570457994644658
SH_C3_3 = 0.3731763325901154
SH_C3_4 = -0.4570457994644658
SH_C3_5 = 1.445305721320277
SH_C3_6 = -0.5900435899266435
CMAP_JET = plt.get_cmap("jet")
CMAP_MIN_NORM, CMAP_MAX_NORM = 5.0, 7.0

class CowDataset(Dataset):

    def __init__(self, root, split):
        super().__init__()
        self.root = root
        self.split = split
        if self.split not in ("train", "test"):
            raise ValueError(f"Invalid split: {self.split}")

        self.masks = []
        self.points = []
        self.images = []
        self.cameras = []

        imgs_root = os.path.join(root, "imgs")
        poses_root = os.path.join(root, "poses")
        points_root = os.path.join(root, "points")
        self.points_path = os.path.join(points_root, "points_10000.npy")

        data_img_size = None
        num_files = len(os.listdir(imgs_root))
        test_idxs = np.linspace(0, num_files, 7).astype(np.int32)[1:-1]
        test_idxs_set = set(test_idxs.tolist())
        train_idxs = [i for i in range(num_files) if i not in test_idxs_set]
        idxs = train_idxs if self.split == "train" else test_idxs

        for i in idxs:
            img_path = os.path.join(imgs_root, f"{i:03d}.png")
            npy_path = os.path.join(poses_root, f"{i:03d}.npy")

            img_ = imageio.v3.imread(img_path).astype(np.float32) / 255.0

            mask = None
            if img_.shape[-1] == 3:
                img = torch.tensor(img_)  # (H, W, 3)
            else:
                img = torch.tensor(img_[..., :3])  # (H, W, 3)
                mask = torch.tensor(img_[..., 3:4])  # (H, W, 1)

            img_size = img.shape[:2]
            dim = img_size[0]
            if img_size[0] != img_size[1]:
                raise RuntimeError

            # Checking if all data samples have the same image size
            if data_img_size is None:
                data_img_size = img_size
            else:
                if data_img_size[0] != img_size[0] or data_img_size[1] != img_size[1]:
                    raise RuntimeError

            pose = np.load(npy_path)
            dist, ele, az = pose.flatten()
            R, T = look_at_view_transform(dist, ele, az)

            img_size = img.shape[:2]
            camera = PerspectiveCameras(
                focal_length=5.0 * dim/2.0, in_ndc=False,
                principal_point=((dim/2, dim/2),),
                R=R, T=T, image_size=(img_size,),
            )

            self.images.append(img)
            self.cameras.append(camera)
            if mask is not None:
                self.masks.append(mask)

        self.img_size = data_img_size

    def __len__(self):
        return len(self.images)

    def __getitem__(self, idx):
        masks = None
        if len(self.masks) > 0:
            masks = self.masks[idx]
        return self.images[idx], self.cameras[idx], masks

    @staticmethod
    def collate_fn(batch):
        images = torch.stack([x[0] for x in batch], dim=0)
        cameras = [x[1] for x in batch]

        masks = [x[2] for x in batch if x[2] is not None]
        if len(masks) == 0:
            masks = None
        else:
            masks = torch.stack(masks, dim=0)

        return images, cameras, masks

def colour_depth_q1_render(depth):
    normalized_depth = (depth - CMAP_MIN_NORM) / (CMAP_MAX_NORM - CMAP_MIN_NORM + 1e-8)
    coloured_depth = CMAP_JET(normalized_depth)[:, :, :3]  # (H, W, 3)
    coloured_depth = (np.clip(coloured_depth, 0.0, 1.0) * 255.0).astype(np.uint8)

    return coloured_depth

def visualize_renders(scene, gt_viz_img, cameras, img_size):

    imgs = []
    viz_size = (256, 256)
    with torch.no_grad():
        for cam in cameras:
            pred_img, _, _ = scene.render(
                cam, img_size=img_size,
                bg_colour=(0.0, 0.0, 0.0),
                per_splat=-1,
            )
            img = torch.clamp(pred_img, 0.0, 1.0) * 255.0
            img = img.clone().detach().cpu().numpy().astype(np.uint8)

            if img_size[0] != viz_size[0] or img_size[1] != viz_size[1]:
                img = np.array(Image.fromarray(img).resize(viz_size))

            imgs.append(img)

    pred_viz_img = np.concatenate(imgs, axis=1)
    viz_frame = np.concatenate((pred_viz_img, gt_viz_img), axis=0)
    return viz_frame

def load_gaussians_from_ply(path):
    # Modified from https://github.com/thomasantony/splat
    max_sh_degree = 3
    plydata = PlyData.read(path)
    xyz = np.stack((np.asarray(plydata.elements[0]["x"]),
                    np.asarray(plydata.elements[0]["y"]),
                    np.asarray(plydata.elements[0]["z"])),  axis=1)
    opacities = np.asarray(plydata.elements[0]["opacity"])[..., np.newaxis]

    features_dc = np.zeros((xyz.shape[0], 3, 1))
    features_dc[:, 0, 0] = np.asarray(plydata.elements[0]["f_dc_0"])
    features_dc[:, 1, 0] = np.asarray(plydata.elements[0]["f_dc_1"])
    features_dc[:, 2, 0] = np.asarray(plydata.elements[0]["f_dc_2"])

    extra_f_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("f_rest_")]
    extra_f_names = sorted(extra_f_names, key = lambda x: int(x.split('_')[-1]))
    assert len(extra_f_names) == 3 * (max_sh_degree + 1) ** 2 - 3
    features_extra = np.zeros((xyz.shape[0], len(extra_f_names)))
    for idx, attr_name in enumerate(extra_f_names):
        features_extra[:, idx] = np.asarray(plydata.elements[0][attr_name])
    # Reshape (P,F*SH_coeffs) to (P, F, SH_coeffs except DC)
    features_extra = features_extra.reshape((features_extra.shape[0], 3, (max_sh_degree + 1) ** 2 - 1))
    features_extra = np.transpose(features_extra, [0, 2, 1])

    scale_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("scale_")]
    scale_names = sorted(scale_names, key = lambda x: int(x.split('_')[-1]))
    scales = np.zeros((xyz.shape[0], len(scale_names)))
    for idx, attr_name in enumerate(scale_names):
        scales[:, idx] = np.asarray(plydata.elements[0][attr_name])

    rot_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("rot")]
    rot_names = sorted(rot_names, key = lambda x: int(x.split('_')[-1]))
    rots = np.zeros((xyz.shape[0], len(rot_names)))
    for idx, attr_name in enumerate(rot_names):
        rots[:, idx] = np.asarray(plydata.elements[0][attr_name])

    xyz = xyz.astype(np.float32)
    rots = rots.astype(np.float32)
    scales = scales.astype(np.float32)
    opacities = opacities.astype(np.float32)
    shs = np.concatenate([
        features_dc.reshape(-1, 3),
        features_extra.reshape(len(features_dc), -1)
    ], axis=-1).astype(np.float32)
    shs = shs.astype(np.float32)

    dc_vals = shs[:, :3]
    dc_colours = np.maximum(dc_vals * SH_C0 + 0.5, np.zeros_like(dc_vals))

    output = {
        "xyz": xyz, "rot": rots, "scale": scales,
        "sh": shs, "opacity": opacities, "dc_colours": dc_colours
    }
    return output

def colours_from_spherical_harmonics(spherical_harmonics, gaussian_dirs):
    """
    [Q 1.3.1] Computes view-dependent colour given spherical harmonic coefficients
    and direction vectors for each gaussian.

    Args:
        spherical_harmonics     :   A torch.Tensor of shape (N, 48) representing the
                                    spherical harmonic coefficients.
        gaussian_dirs           :   A torch.Tensor of shape (N, 3) representing the
                                    direction vectors pointing from the camera origin
                                    to each Gaussian.

    Returns:
        colours                 :   A torch.Tensor of shape (N, 3) representing the view dependent
                                    RGB colour.
    """
    ### YOUR CODE HERE ###
    # c0 = spherical_harmonics[:, 0:3]
    c = spherical_harmonics[:, 0:48].view(spherical_harmonics.shape[0], 16, 3)

    x = gaussian_dirs[:, [0]] # (N,1)
    y = gaussian_dirs[:, [1]]
    z = gaussian_dirs[:, [2]]
    # xx = torch.mul(x, x)
    # yy = torch.mul(y, y)
    # zz = torch.mul(z, z)
    # xy = torch.mul(x, y)
    # yz = torch.mul(y, z)
    # xz = torch.mul(x, z)
    xx = x * x
    yy = y * y
    zz = z * z
    xy = x * y
    yz = y * z
    xz = x * z
    print ("x shape", x.shape)

    # (1, 14, 1)
    cf =  torch.tensor([SH_C0,SH_C1,SH_C2_0,SH_C2_1, SH_C2_2, SH_C2_3, SH_C2_4, 
                                 SH_C3_0, SH_C3_1, SH_C3_2, SH_C3_3, SH_C3_4, SH_C3_5, SH_C3_6], device=spherical_harmonics.device).view(1,14,1)

    colours = (
        cf[:, 0] * c[:, 0] - \
        cf[:, 1] * y * c[:, 1] + cf[:,1] * z * c[:,2] - cf[:,1] * x * c[:,3] + \
        cf[:, 2] * xy * c[:,4] + cf[:,3] * yz * c[:,5] + cf[:,4] * (2.0*zz-xx-yy) * c[:,6] + cf[:,5] * xz * c[:,7] + cf[:,6] * (xx-yy) * c[:,8] + \
        cf[:, 7] * y * (3.0 * xx - yy) * c[:, 9] +  cf[:, 8] * xy * z * c[:, 10] + cf[:, 9] * y * (4.0 * zz - xx - yy) * c[:, 11] + \
        cf[:, 10] * z * (2.0 * zz - 3.0 * xx - 3.0 * yy) * c[:, 12] + cf[:, 11] * x * (4.0 * zz - xx - yy) * c[:, 13] + cf[:, 12] * z * (xx - yy) * c[:, 14] + \
        cf[:, 13] * x * (xx - 3.0 - yy) * c[:, 15]
        )
    # colours = torch.sum(colours, dim=1)
    colours = torch.clamp(colours + 0.5, 0.0, 1.0)
    print("colours", colours.shape)
    return colours
