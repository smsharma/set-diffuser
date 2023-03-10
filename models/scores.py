import dataclasses

import jax
import jax.numpy as np
import flax.linen as nn
import jraph
import e3nn_jax as e3nn

from models.transformer import Transformer
from models.gnn import GraphConvNet
from models.equivariant_transformer import EquivariantTransformer
from models.mlp import MLP

from models.graph_utils import nearest_neighbors
from models.diffusion_utils import get_timestep_embedding


class TransformerScoreNet(nn.Module):
    """Transformer score network."""

    d_t_embedding: int = 32
    score_dict: dict = dataclasses.field(default_factory=lambda: {"d_model": 256, "d_mlp": 512, "n_layers": 4, "n_heads": 4})

    @nn.compact
    def __call__(self, z, t, conditioning, mask):

        assert np.isscalar(t) or len(t.shape) == 0 or len(t.shape) == 1
        t = t * np.ones(z.shape[0])  # Ensure t is a vector

        t_embedding = get_timestep_embedding(t, self.d_t_embedding)  # Timestep embeddings

        if conditioning is not None:
            cond = np.concatenate([t_embedding, conditioning], axis=1)  # Concatenate with conditioning context
        else:
            cond = t_embedding

        # Pass context through a 2-layer MLP before passing into transformer
        # I'm not sure this is really necessary
        d_cond = cond.shape[-1]  # Dimension of conditioning context
        cond = MLP([d_cond * 4, d_cond * 4, d_cond])(cond)

        # Make copy of score dict since original cannot be in-place modified; remove `score` argument before passing to Net
        score_dict = dict(self.score_dict)
        score_dict.pop("score", None)

        h = Transformer(n_input=z.shape[-1], **score_dict)(z, cond, mask)

        return z + h


class GraphScoreNet(nn.Module):
    """Graph-convolutional score network."""

    d_t_embedding: int = 32
    score_dict: dict = dataclasses.field(default_factory=lambda: {"k": 20, "num_mlp_layers": 4, "latent_size": 128, "skip_connections": True, "message_passing_steps": 4})

    @nn.compact
    def __call__(self, z, t, conditioning, mask):

        assert np.isscalar(t) or len(t.shape) == 0 or len(t.shape) == 1
        t = t * np.ones(z.shape[0])  # Ensure t is a vector

        t_embedding = get_timestep_embedding(t, self.d_t_embedding)  # Timestep embeddings

        if conditioning is not None:
            cond = np.concatenate([t_embedding, conditioning], axis=1)  # Concatenate with conditioning context
        else:
            cond = t_embedding

        # Pass context through a 2-layer MLP before passing into transformer
        # I'm not sure this is really necessary
        d_cond = cond.shape[-1]  # Dimension of conditioning context
        cond = MLP([d_cond * 4, d_cond * 4, d_cond])(cond)
        k = self.score_dict["k"]
        n_pos_features = self.score_dict["n_pos_features"]

        sources, targets = jax.vmap(nearest_neighbors, in_axes=(0, None))(z[..., :n_pos_features], k, mask=mask)
        n_batch = z.shape[0]
        graph = jraph.GraphsTuple(
            n_node=(mask.sum(-1)[:, None]).astype(np.int32), 
            n_edge=np.array(n_batch * [[k]]), 
            nodes=z, 
            edges=None, 
            globals=cond, 
            senders=sources, 
            receivers=targets,
        )

        # Make copy of score dict since original cannot be in-place modified; remove `k` argument before passing to Net
        score_dict = dict(self.score_dict)
        score_dict.pop("k", None)
        score_dict.pop("score", None)
        score_dict.pop("n_pos_features", None)

        h = jax.vmap(GraphConvNet(**score_dict))(graph)
        h = h.nodes

        return z + h


class EquivariantTransformereNet(nn.Module):
    """Equivariant transformer score network. NOTE: Does not currently support masking."""

    d_t_embedding: int = 32
    score_dict: dict = dataclasses.field(default_factory=lambda: {"k": 20})

    @nn.compact
    def __call__(self, z, t, conditioning, mask):

        assert np.isscalar(t) or len(t.shape) == 0 or len(t.shape) == 1
        t = t * np.ones(z.shape[0])  # Ensure t is a vector

        t_embedding = get_timestep_embedding(t, self.d_t_embedding)  # Timestep embeddings

        if conditioning is not None:
            cond = np.concatenate([t_embedding, conditioning], axis=1)  # Concatenate with conditioning context
        else:
            cond = t_embedding

        # Pass context through a 2-layer MLP before passing into transformer
        # I'm not sure this is really necessary
        d_cond = cond.shape[-1]  # Dimension of conditioning context
        cond = MLP([d_cond * 4, d_cond * 4, d_cond])(cond)

        k = self.score_dict["k"]
        n_pos_features = self.score_dict["n_pos_features"]

        # Isolate positions, velocities, and masses; get nearest neighbor edges.
        pos, vel, mass = z[..., :n_pos_features], z[..., n_pos_features : 2 * n_pos_features], z[..., 2 * n_pos_features :]
        sources, targets = jax.vmap(nearest_neighbors, in_axes=(0, None))(z[..., :n_pos_features], k)

        feat = np.concatenate([vel, mass], -1)

        # Position and feature irreps arrays. Add the mass to the conditioning vectors.
        pos = e3nn.IrrepsArray("1o", pos)
        feat = e3nn.IrrepsArray(f"1o + {d_cond}x0e", np.concatenate([vel, mass + cond[:, None, :]], -1))

        # Make copy of score dict since original cannot be in-place modified; remove `k` argument before passing to Net
        score_dict = dict(self.score_dict)
        score_dict.pop("k", None)
        score_dict.pop("score", None)
        score_dict.pop("n_pos_features", None)

        pos, feat = jax.vmap(EquivariantTransformer(irreps_out="1o + 0e", **score_dict))(pos, feat, sources, targets)

        h = np.concatenate([pos.array, feat.array], -1)

        return z + h
