import torch
import numpy as np
import ipdb as pdb
import torch.nn as nn
import torch.nn.init as init
import pytorch_lightning as pl
import torch.distributions as D
from torch.nn import functional as F
from components.beta import BetaVAE_MLP
from components.transforms import NormalizingFlow
from metrics.correlation import compute_mcc
from metrics.block import compute_r2

class SSA(pl.LightningModule):

    def __init__(
        self, 
        input_dim,
        c_dim,
        s_dim, 
        nclass,
        n_flow_layers,
        optimizer="adam",
        embedding_dim=0,
        hidden_dim=128,
        bound=5,
        count_bins=8,
        order='linear',
        lr=1e-4,
        beta=0.0025,
        gamma=0.001,
        sigma=1e-6,
        sigma_x=0.1,
        sigma_y=None,
        vae_slope=0.2,
        use_warm_start=False,
        spline_pth=None,
        decoder_dist='gaussian',
        correlation='Pearson',
        encoder_n_layers=3,
        decoder_n_layers=1,
        scheduler=None,
        lr_factor=0.5,
        lr_patience=10,
        hz_to_z=True,
    ):
        '''Stationary subspace analysis'''
        super().__init__()
        self.c_dim = c_dim
        self.s_dim = s_dim
        self.z_dim = c_dim + s_dim
        self.input_dim = input_dim
        self.lr = lr
        self.nclass = nclass
        self.beta = beta
        self.gamma = gamma
        self.sigma = sigma
        self.correlation = correlation
        self.decoder_dist = decoder_dist
        self.embedding_dim = embedding_dim
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.lr_factor = lr_factor
        self.lr_patience = lr_patience
        self.best_r2 = 0.
        self.best_mcc = 0.
        self.best_sum = 0.
        self.best_sum_mcc = 0.
        self.best_sum_r2 = 0.
        self.r2_at_best_mcc = 0.
        self.mcc_at_best_r2 = 0.
        self.hz_to_z = hz_to_z

        # embedding of the domain index
        if self.embedding_dim > 0:
            self.embeddings = nn.Embedding(self.nclass, self.embedding_dim)

        # Inference
        self.net = BetaVAE_MLP(input_dim=self.input_dim+self.embedding_dim, 
                               output_dim=self.input_dim,
                               z_dim=self.z_dim, 
                               slope=vae_slope,
                               encoder_n_layers=encoder_n_layers,
                               decoder_n_layers=decoder_n_layers,
                               hidden_dim=hidden_dim)

        
        # Spline flow model to learn the noise distribution
        self.spline_list = []
        for i in range(self.nclass):
            spline = NormalizingFlow(
                input_dim=s_dim,
                n_layers=n_flow_layers,
                bound=bound,
                count_bins=count_bins,
                order=order,
            )

            if use_warm_start:
                spline.load_state_dict(torch.load(spline_pth, 
                                                  map_location=torch.device('cpu')))

                print("Load pretrained spline flow", flush=True)
            self.spline_list.append(spline)
        self.spline_list = nn.ModuleList(self.spline_list)

        # base distribution for calculation of log prob under the model
        self.register_buffer('base_dist_mean', torch.zeros(self.s_dim))
        self.register_buffer('base_dist_var', torch.eye(self.s_dim))

    @property
    def base_dist(self):
        return D.MultivariateNormal(self.base_dist_mean, self.base_dist_var)
    
    def reparameterize(self, mean, logvar, random_sampling=True):
        if random_sampling:
            eps = torch.randn_like(logvar)
            std = torch.exp(0.5*logvar)
            z = mean + eps*std
            return z
        else:
            return mean

    def reconstruction_loss(self, x, x_recon, distribution):
        batch_size = x.size(0)
        assert batch_size != 0

        if distribution == 'bernoulli':
            recon_loss = F.binary_cross_entropy_with_logits(
                x_recon, x, size_average=False).div(batch_size)

        elif distribution == 'gaussian':
            recon_loss = F.mse_loss(x_recon, x, size_average=False).div(batch_size)

        elif distribution == 'sigmoid_gaussian':
            x_recon = F.sigmoid(x_recon)
            recon_loss = F.mse_loss(x_recon, x, size_average=False).div(batch_size)

        return recon_loss

    def forward(self, batch):
        x, c = batch['x'], batch['c']
        if self.embedding_dim > 0:
            x = torch.cat([x, self.embeddings(c.squeeze().long())], dim=1)
        _, mus, logvars, zs = self.net(x)
        return zs, mus, logvars       

    def training_step(self, batch, batch_idx):
        x, c = batch['x'], batch['c']
        batch_size, _ = x.shape
        if self.embedding_dim > 0:
            x = torch.cat([x, self.embeddings(c.squeeze().long())], dim=1)
        c = torch.squeeze(c).to(torch.int64)
        x_recon, mus, logvars, zs = self.net(x)
        # VAE ELBO loss: recon_loss + kld_loss
        recon_loss = self.reconstruction_loss(x, x_recon, self.decoder_dist)
        q_dist = D.Normal(mus, torch.exp(logvars / 2))
        log_qz = q_dist.log_prob(zs)
        # Content KLD
        p_dist = D.Normal(torch.zeros_like(mus[:,:self.c_dim]), torch.ones_like(logvars[:,:self.c_dim]))
        log_pz_content = torch.sum(p_dist.log_prob(zs[:,:self.c_dim]),dim=-1)
        log_qz_content = torch.sum(log_qz[:,:self.c_dim],dim=-1)
        kld_content = log_qz_content - log_pz_content
        kld_content = kld_content.mean()
        # Style KLD
        log_qz_style = log_qz[:,self.c_dim:]
        residuals = zs[:,self.c_dim:]
        sum_log_abs_det_jacobians = 0
        one_hot = F.one_hot(c, num_classes=self.nclass)
        # Nonstationary branch
        es = [ ]
        logabsdet = [ ]
        for c in range(self.nclass):
            es_c, logabsdet_c = self.spline_list[c](residuals)
            es.append(es_c)
            logabsdet.append(logabsdet_c)
        es = torch.stack(es, axis=1)
        logabsdet = torch.stack(logabsdet, axis=1)
        mask = one_hot.reshape(-1, self.nclass)
        es = (es * mask.unsqueeze(-1)).sum(1)
        logabsdet = (logabsdet * mask).sum(1)
        es = es.reshape(batch_size, self.s_dim)
        sum_log_abs_det_jacobians = sum_log_abs_det_jacobians + logabsdet
        log_pz_style = self.base_dist.log_prob(es) + sum_log_abs_det_jacobians
        kld_style = torch.sum(log_qz_style, dim=-1) - log_pz_style
        kld_style = kld_style.mean()
        # VAE training
        loss = recon_loss + self.beta * kld_content + self.gamma * kld_style# + self.sigma * hsic_loss
        self.log("train_elbo_loss", loss)
        self.log("train_recon_loss", recon_loss)
        self.log("train_kld_content", kld_content)
        self.log("train_kld_style", kld_style)
        return loss

    def validation_step(self, batch, batch_idx):
        x, c, y = batch['x'], batch['c'], batch['y']
        batch_size, _ = x.shape
        if self.embedding_dim > 0:
            x = torch.cat([x, self.embeddings(c.squeeze().long())], dim=1)
        c = torch.squeeze(c).to(torch.int64)
        x_recon, mus, logvars, zs = self.net(x)

        # VAE ELBO loss: recon_loss + kld_loss
        recon_loss = self.reconstruction_loss(x, x_recon, self.decoder_dist)
        q_dist = D.Normal(mus, torch.exp(logvars / 2))
        log_qz = q_dist.log_prob(zs)

        # Content KLD
        p_dist = D.Normal(torch.zeros_like(mus[:,:self.c_dim]), torch.ones_like(logvars[:,:self.c_dim]))
        log_pz_content = torch.sum(p_dist.log_prob(zs[:,:self.c_dim]),dim=-1)
        log_qz_content = torch.sum(log_qz[:,:self.c_dim],dim=-1)
        kld_content = log_qz_content - log_pz_content
        kld_content = kld_content.mean()

        # Style KLD
        log_qz_style = log_qz[:,self.c_dim:]
        residuals = zs[:,self.c_dim:]
        sum_log_abs_det_jacobians = 0
        one_hot = F.one_hot(c, num_classes=self.nclass)
        # Nonstationary branch
        es = [ ]
        logabsdet = [ ]
        for c in range(self.nclass):
            es_c, logabsdet_c = self.spline_list[c](residuals)
            es.append(es_c)
            logabsdet.append(logabsdet_c)
        es = torch.stack(es, axis=1)
        logabsdet = torch.stack(logabsdet, axis=1)
        mask = one_hot.reshape(-1, self.nclass)
        es = (es * mask.unsqueeze(-1)).sum(1)
        logabsdet = (logabsdet * mask).sum(1)
        es = es.reshape(batch_size, self.s_dim)
        sum_log_abs_det_jacobians = sum_log_abs_det_jacobians + logabsdet
        log_pz_style = self.base_dist.log_prob(es) + sum_log_abs_det_jacobians
        kld_style = torch.sum(log_qz_style, dim=-1) - log_pz_style
        kld_style = kld_style.mean()

        # VAE training
        loss = recon_loss + self.beta * kld_content + self.gamma * kld_style# + self.sigma * hsic_loss

        # Compute Kernel Regression R^2
        if self.hz_to_z is False:
            r2 = compute_r2(mus[:,:self.c_dim], y[:,:self.c_dim])
        else:
            r2 = compute_r2(y[:,:self.c_dim], mus[:,:self.c_dim])
        # Compute Mean Correlation Coefficient (MCC)
        zt_recon = mus[:,self.c_dim:].T.detach().cpu().numpy()
        zt_true = y[:,self.c_dim:].T.detach().cpu().numpy()
        mcc = compute_mcc(zt_recon, zt_true, self.correlation)

        self.log("val_mcc", mcc)
        self.log("val_r2", r2)  
        self.log("val_elbo_loss", loss)
        self.log("val_recon_loss", recon_loss)
        self.log("val_kld_content", kld_content)
        self.log("val_kld_style", kld_style)

        if r2 >= self.best_r2:
            self.best_r2 = r2
            self.mcc_at_best_r2 = mcc
        self.log("best_r2", self.best_r2)
        self.log("mcc_at_best_r2", self.mcc_at_best_r2)
        
        if mcc >= self.best_mcc:
            self.best_mcc = mcc
            self.r2_at_best_mcc = r2
        self.log("best_mcc", self.best_mcc)
        self.log("r2_at_best_mcc", self.r2_at_best_mcc)

        if mcc + r2 >= self.best_sum:
            self.best_sum = mcc + r2
            self.best_sum_mcc = mcc
            self.best_sum_r2 = r2
        self.log("best_sum_mcc", self.best_sum_mcc)
        self.log("best_sum_r2", self.best_sum_r2)

        self.val_r2 = r2

        return loss
    
    def sample(self, n=64):
        with torch.no_grad():
            e = torch.randn(n, self.z_dim, device=self.device)
            eps, _ = self.spline.inverse(e)
        return eps

    def reconstruct(self, batch):
        zs, mus, logvars = self.forward(batch)
        zs_flat = zs.contiguous().view(-1, self.z_dim)
        x_recon = self.dec(zs_flat)
        x_recon = x_recon.view(batch_size, self.length, self.input_dim)       
        return x_recon

    def configure_optimizers(self):
        if self.optimizer.lower() == "adam":
            opt_v = torch.optim.AdamW(filter(lambda p: p.requires_grad, self.parameters()), lr=self.lr, betas=(0.9, 0.999), weight_decay=0.0001)
        elif self.optimizer.lower() == "rmsprop":
            opt_v = torch.optim.RMSprop(filter(lambda p: p.requires_grad, self.parameters()), lr=self.lr)
        else: 
            raise NotImplementedError
        return [opt_v], []
