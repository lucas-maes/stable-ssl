"""SSL losses."""

#
# Author: Hugues Van Assel <vanasselhugues@gmail.com>
#         Randall Balestriero <randallbalestriero@gmail.com>
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import torch
import torch.nn.functional as F
from loguru import logger as logging

from .utils import all_gather, all_reduce


def mae(target, pred, mask, norm_pix_loss=False):
    """Compute masked autoencoder loss.

    Args:
        target: [N, L, p*p*3] target images
        pred: [N, L, p*p*3] predicted images
        mask: [N, L], 0 is keep, 1 is remove
        norm_pix_loss: whether to normalize pixels

    Returns:
        loss: mean loss value
    """
    if norm_pix_loss:
        mean = target.mean(dim=-1, keepdim=True)
        var = target.var(dim=-1, keepdim=True)
        target = (target - mean) / (var + 1.0e-6) ** 0.5

    loss = (pred - target) ** 2
    loss = loss.mean(dim=-1)  # [N, L], mean loss per patch

    loss = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
    return loss


def off_diagonal(x):
    """Return a flattened view of the off-diagonal elements of a square matrix."""
    n, m = x.shape
    assert n == m, logging.error("Input tensor must be square.")
    return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()


class NTXEntLoss(torch.nn.Module):
    """Normalized temperature-scaled cross entropy loss.

    Introduced in the SimCLR paper :cite:`chen2020simple`.
    Also used in MoCo :cite:`he2020momentum`.

    Args:
        temperature (float, optional): The temperature scaling factor.
            Default is 0.5.
    """

    def __init__(self, temperature: float = 0.5):
        super().__init__()
        self.temperature = temperature

    def forward(self, z_i, z_j):
        """Compute the NT-Xent loss.

        Args:
            z_i (torch.Tensor): Latent representation of the first augmented view of the batch.
            z_j (torch.Tensor): Latent representation of the second augmented view of the batch.

        Returns:
            float: The computed contrastive loss.
        """
        z_i = torch.cat(all_gather(z_i), 0)
        z_j = torch.cat(all_gather(z_j), 0)

        z = torch.cat([z_i, z_j], 0)
        N = z.size(0)

        features = F.normalize(z, dim=1)
        sim = torch.matmul(features, features.T) / self.temperature

        sim_i_j = torch.diag(sim, N // 2)
        sim_j_i = torch.diag(sim, -N // 2)

        positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0)

        mask = torch.eye(N, dtype=bool).to(z_i.device)
        negative_samples = sim[~mask].reshape(N, -1)

        attraction = -positive_samples.mean()
        repulsion = torch.logsumexp(negative_samples, dim=1).mean()

        return attraction + repulsion


class NegativeCosineSimilarity(torch.nn.Module):
    """Negative cosine similarity objective.

    This objective is used for instance in BYOL :cite:`grill2020bootstrap`
    or SimSiam :cite:`chen2021exploring`.
    """

    def forward(self, z_i, z_j):
        """Compute the loss of the BYOL model.

        Args:
            z_i (torch.Tensor): Latent representation of the first augmented view of the batch.
            z_j (torch.Tensor): Latent representation of the second augmented view of the batch.

        Returns:
            float: The computed loss.
        """
        sim = torch.nn.CosineSimilarity(dim=1)
        return -sim(z_i, z_j).mean()


class VICRegLoss(torch.nn.Module):
    """SSL objective used in VICReg :cite:`bardes2021vicreg`.

    Args:
        sim_coeff (float, optional): The weight of the similarity loss (attractive term).
            Default is 25.
        std_coeff (float, optional): The weight of the standard deviation loss.
            Default is 25.
        cov_coeff (float, optional): The weight of the covariance loss.
            Default is 1.
        epsilon (float, optional): Small value to avoid division by zero.
            Default is 1e-4.
    """

    def __init__(
        self,
        sim_coeff: float = 25,
        std_coeff: float = 25,
        cov_coeff: float = 1,
        epsilon: float = 1e-4,
    ):
        super().__init__()
        self.sim_coeff = sim_coeff
        self.std_coeff = std_coeff
        self.cov_coeff = cov_coeff
        self.epsilon = epsilon

    def forward(self, z_i, z_j):
        """Compute the loss of the VICReg model.

        Args:
            z_i (torch.Tensor): Latent representation of the first augmented view of the batch.
            z_j (torch.Tensor): Latent representation of the second augmented view of the batch.

        Returns:
            float: The computed loss.
        """
        repr_loss = F.mse_loss(z_i, z_j)

        z_i = all_gather(z_i)
        z_j = all_gather(z_j)

        z_i = z_i - z_i.mean(dim=0)
        z_j = z_j - z_j.mean(dim=0)

        std_i = torch.sqrt(z_i.var(dim=0) + self.epsilon)
        std_j = torch.sqrt(z_j.var(dim=0) + self.epsilon)
        std_loss = torch.mean(F.relu(1 - std_i)) / 2 + torch.mean(F.relu(1 - std_j)) / 2

        cov_i = (z_i.T @ z_i) / (z_i.size(0) - 1)
        cov_j = (z_j.T @ z_j) / (z_i.size(0) - 1)
        cov_loss = off_diagonal(cov_i).pow_(2).sum().div(z_i.size(1)) + off_diagonal(
            cov_j
        ).pow_(2).sum().div(z_i.size(1))

        loss = (
            self.sim_coeff * repr_loss
            + self.std_coeff * std_loss
            + self.cov_coeff * cov_loss
        )
        return loss


class BarlowTwinsLoss(torch.nn.Module):
    """SSL objective used in Barlow Twins :cite:`zbontar2021barlow`.

    Args:
        lambd (float, optional): The weight of the off-diagonal terms in the loss.
            Default is 5e-3.
    """

    def __init__(self, lambd: float = 5e-3):
        super().__init__()
        self.lambd = lambd
        self.bn = torch.nn.LazyBatchNorm1d()

    def forward(self, z_i, z_j):
        """Compute the loss of the Barlow Twins model.

        Args:
            z_i (torch.Tensor): Latent representation of the first augmented view of the batch.
            z_j (torch.Tensor): Latent representation of the second augmented view of the batch.

        Returns:
            float: The computed loss.
        """
        c = self.bn(z_i).T @ self.bn(z_j)  # normalize along the batch dimension
        c = c / z_i.size(0)
        all_reduce(c)

        on_diag = (torch.diagonal(c) - 1).pow(2).sum()
        off_diag = off_diagonal(c).pow(2).sum()
        loss = on_diag + self.lambd * off_diag
        return loss
