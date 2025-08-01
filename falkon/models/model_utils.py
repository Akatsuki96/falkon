import warnings
from abc import ABC, abstractmethod
from typing import Optional, Tuple, Union

import numpy as np
import torch
from sklearn import base

import falkon
from falkon import FalkonOptions
from falkon.kernels.keops_helpers import should_use_keops
from falkon.sparse import SparseTensor
from falkon.utils import check_random_generator, decide_cuda, devices
from falkon.utils.helpers import check_same_dtype, sizeof_dtype
from falkon.utils.tensor_helpers import is_f_contig

_tensor_type = Union[torch.Tensor, SparseTensor]


class FalkonBase(base.BaseEstimator, ABC):
    def __init__(
        self,
        kernel: falkon.kernels.Kernel,
        M: Optional[int],
        center_selection: Union[str, falkon.center_selection.CenterSelector] = "uniform",
        seed: Optional[int] = None,
        error_fn: Optional[callable] = None,
        error_every: Optional[int] = 1,
        options: Optional[FalkonOptions] = None,
    ):
        self.kernel = kernel
        self.M = M
        self.seed = seed
        if self.seed is not None:
            torch.manual_seed(self.seed)  # Works for both CPU and GPU
        self.random_state_ = check_random_generator(self.seed)

        self.error_fn = error_fn
        self.error_every = error_every
        # Options
        self.options = options or FalkonOptions()

        self.use_cuda_ = decide_cuda(self.options)
        self.num_gpus = 0
        self.alpha_ = None
        self.ny_points_ = None
        self.fit_times_ = None
        self.val_errors_ = None

        self.center_selection = self._init_center_selection(center_selection)

    def _init_center_selection(
        self, center_selection: Union[str, falkon.center_selection.CenterSelector]
    ) -> falkon.center_selection.CenterSelector:
        if isinstance(center_selection, str):
            if center_selection.lower() == "uniform":
                if self.M is None:
                    raise ValueError(
                        "M must be specified when no `CenterSelector` object is provided. "
                        "Specify an integer value for `M` or a `CenterSelector` object."
                    )
                return falkon.center_selection.UniformSelector(self.random_state_, num_centers=self.M)
            else:
                raise ValueError(f'Center selection "{center_selection}" is not valid.')
        return center_selection

    def _init_cuda(self):
        if self.use_cuda_:
            torch.cuda.init()
            self.num_gpus = devices.num_gpus(self.options)

    def _reset_state(self):
        self.alpha_ = None
        self.ny_points_ = None
        self.fit_times_ = []
        self.val_errors_ = []

    def _get_callback_fn(
        self,
        X: Optional[_tensor_type],
        Y: Optional[torch.Tensor],
        Xts: Optional[_tensor_type],
        Yts: Optional[torch.Tensor],
        ny_points: _tensor_type,
        precond: falkon.preconditioner.Preconditioner,
    ):
        """Returns the callback function for CG iterations.

        The callback computes and displays the validation error.
        """
        assert not (X is None and Xts is None), "At least one of `X` or `Xts` must be specified"
        assert not (Y is None and Yts is None), "At least one of `Y` or `Yts` must be specified"
        assert self.error_fn is not None, "Error function must be specified for callbacks"

        def val_cback(it, beta, train_time):
            assert self.error_fn is not None
            assert self.fit_times_ is not None
            assert self.val_errors_ is not None
            # fit_times_[0] is the preparation (i.e. preconditioner time).
            # train_time is the cumulative training time (excludes time for this function)
            self.fit_times_.append(self.fit_times_[0] + train_time)
            if it % self.error_every != 0:
                print(f"Iteration {it:3d} - Elapsed {self.fit_times_[-1]:.2f}s", flush=True)
                return
            err_str = "training" if Xts is None or Yts is None else "validation"
            alpha = self._params_to_original_space(beta, precond)
            # Compute error: can be train or test
            if Xts is not None and Yts is not None:
                pred = self._predict(Xts, ny_points, alpha)
                err = self.error_fn(Yts, pred)
            else:
                assert X is not None and Y is not None
                pred = self._predict(X, ny_points, alpha)
                err = self.error_fn(Y, pred)
            err_name = "error"
            if isinstance(err, tuple) and len(err) == 2:
                err, err_name = err
            print(
                f"Iteration {it:3d} - Elapsed {self.fit_times_[-1]:.2f}s - {err_str} {err_name}: {str(err)}",
                flush=True,
            )
            self.val_errors_.append(err)

        return val_cback

    def _check_fit_inputs(
        self, X: _tensor_type, Y: torch.Tensor, Xts: _tensor_type, Yts: torch.Tensor
    ) -> Tuple[_tensor_type, torch.Tensor, _tensor_type, torch.Tensor]:
        if X.shape[0] != Y.shape[0]:
            raise ValueError(f"X and Y must have the same number of samples (found {X.shape[0]} and {Y.shape[0]})")
        if Y.dim() == 1:
            Y = torch.unsqueeze(Y, 1)
        if Y.dim() != 2:
            raise ValueError(f"Y is expected 1D or 2D. Found {Y.dim()}D.")
        if not check_same_dtype(X, Y):
            raise TypeError("X and Y must have the same data-type.")

        # If KeOps is used, data must be C-contiguous.
        if should_use_keops(X, X, self.options):
            X = to_c_contig(X, "X", True)
            Y = to_c_contig(Y, "Y", True)
            Xts = to_c_contig(Xts, "Xts", True)
            Yts = to_c_contig(Yts, "Yts", True)

        return X, Y, Xts, Yts

    def _check_predict_inputs(self, X: _tensor_type) -> _tensor_type:
        if self.alpha_ is None or self.ny_points_ is None:
            raise RuntimeError("Falkon has not been trained. `predict` must be called after `fit`.")
        if should_use_keops(X, self.ny_points_, self.options):
            X = to_c_contig(X, "X", True)

        return X

    def _can_store_knm(self, X, ny_points, available_ram):
        """Decide whether it's worthwile to pre-compute the k_NM kernel.

        Notes
        -----
        If we precompute K_NM, each CG iteration costs
        Given a single kernel evaluation between two D-dimensional vectors
        costs D, at CG iteration we must perform N*M kernel evaluations.
        Other than the kernel evaluations we must perform two matrix-vector
        products 2(N*M*T) and a bunch of triangular solves.

        So if we precompute we have 2*(N*M*T), othewise we also have N*M*D
        but precomputing costs us N*M memory.
        So heuristic is the following:
         - If D is large (> `store_threshold`) check if RAM is sufficient
         - If RAM is sufficient precompute
         - Otherwise do not precompute
        """
        if self.options.never_store_kernel:
            return False
        dts = sizeof_dtype(X.dtype)
        store_threshold = self.options.store_kernel_d_threshold
        if X.size(1) > store_threshold:
            necessary_ram = X.size(0) * ny_points.size(0) * dts
            if available_ram > necessary_ram:
                if self.options.debug:
                    print(f"{X.size(0)}*{ny_points.size(0)} Kernel matrix will be stored")
                return True
            elif self.options.debug:
                print(
                    f"Cannot store full kernel matrix: not enough memory "
                    f"(have {available_ram / 2 ** 30:.2f}GB, need {necessary_ram / 2 ** 30:.2f}GB)"
                )
                return False
        else:
            return False

    @abstractmethod
    def fit(
        self, X: torch.Tensor, Y: torch.Tensor, Xts: Optional[torch.Tensor] = None, Yts: Optional[torch.Tensor] = None
    ):
        pass

    @abstractmethod
    def _predict(self, X, ny_points_, alpha_):
        pass

    @abstractmethod
    def _params_to_original_space(self, params, preconditioner):
        pass

    def predict(self, X: torch.Tensor) -> torch.Tensor:
        """Makes predictions on data `X` using the learned model.

        Parameters
        -----------
        X : torch.Tensor
            Tensor of test data points, of shape [num_samples, num_dimensions].

        Returns
        --------
        predictions : torch.Tensor
            Prediction tensor of shape [num_samples, num_outputs] for all
            data points.
        """
        X = self._check_predict_inputs(X)

        return self._predict(X, self.ny_points_, self.alpha_)

    def __repr__(self, **kwargs):
        return super().__repr__(N_CHAR_MAX=5000)


def to_c_contig(tensor: Optional[torch.Tensor], name: str = "", warn: bool = False) -> Optional[torch.Tensor]:
    warning_text = (
        "Input '%s' is F-contiguous (stride=%s); to ensure KeOps compatibility, C-contiguous inputs "
        "are necessary. The data will be copied to change its order. To avoid this "
        "unnecessary copy, either disable KeOps (passing `keops_active='no'`) or make "
        "the input tensors C-contiguous."
    )
    if tensor is not None and is_f_contig(tensor, strict=True):
        if warn:
            # noinspection PyArgumentList
            warnings.warn(warning_text % (name, tensor.stride()))
        orig_device = tensor.device
        return torch.from_numpy(np.asarray(tensor.cpu().numpy(), order="C")).to(device=orig_device)
    return tensor
