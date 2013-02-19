'''
Locality Preserving Projection (LPP)

References
----------
[1] "X. He and P. Niyogi. Locality preserving projections. Advances in Neural
Information Processing Systems 16 (NIPS 2003), 2003. Vancouver, Canada."

Created on 2012/11/27
@author: du
'''

import numpy as np
from scipy.sparse import csr_matrix
from ..base import BaseEstimator, TransformerMixin
from ..neighbors import kneighbors_graph
from ..utils.extmath import safe_sparse_dot
from ..utils import check_random_state, check_arrays, assert_all_finite
from scipy.linalg import eigh
from ..utils.arpack import eigsh
from ..decomposition import PCA



def adjacency_matrix(X, n_neighbors, mode='distance'):
    """Compute weighted adjacency matrix by k-nearest search

    Parameters
    ----------
    X : array-like, shape: (n_samples, n_features)
        Input data, each column is an edge of a graph

    n_neighbors : int
        number of neighbors to define whether two edges are connected or not

    mode : string
        mode of kneighbors_graph calculation

    use_ext : bool
        whether use cython extension or not

    Returns
    -------
    adjacency matrix : LIL sparse matrix, shape: (n_samples, n_samples)
        (i, j) component of it is defined in the section 2.2 of [1]
    """
    X = np.asanyarray(X)
    G = kneighbors_graph(X, n_neighbors, mode)
    G = G.tolil()
    nzx, nzy = G.nonzero()
    try:
        from _lpp import to_symmetrix_matrix
        G = to_symmetrix_matrix(G, nzx, nzy)
    except ImportError:
        for xx, yy in zip(nzx, nzy):
            if G[yy, xx] == 0:
                G[yy, xx] = G[xx, yy]

    return G


def affinity_matrix(adj_mat, kernel_func="heat", kernel_param="auto"):
    """Compute the affinity matrix from adjacency matrix

    Parameters
    ----------
    adj_mat : array-like, shape(n_samples, n_samples)
        the adjacency matrix

    kernel_func : string or callable
        kernel function to compute affinity matrix

    kernel_param : float
        parameter for heat kernel

    Returns
    -------
    affinity matrix : CSR sparse matrix, shape(n_samples, n_samples)
        (i, j) component W(i,j) is defined in [1] section 2.2 2 (a)
    """
    W = csr_matrix(adj_mat)
    if kernel_func == "heat":
        W.data **= 2

        # Estimate kernel_param by the heuristics that it must correspond to
        # the variance of Gaussian distribution.
        if kernel_param == "auto":
            kernel_param = np.median(W.data)

        np.exp(-W.data / kernel_param, W.data)
    else:
        W.data = kernel_func(W.data)
    return W


def laplacian_matrix(afn_mat):
    """Compute the affinity matrix from adjacency matrix

    Parameters
    ----------
    afn_mat : CSR sparse matrix, shape(n_samples, n_samples)
        the affinity matrix

    Returns
    -------
    Laplacian matrix : CSR sparse matrix, shape(n_samples, n_samples)
        (i, j) component L(i,j) is defined in [1] section 2.2 3

    col_sum : ndarray, shape(n_samples)
        diagonal matrix whose entries are column sums of the affinity matrix
    """
    col_sum = np.asarray(afn_mat.sum(0)).flatten()
    lap_mat = (-afn_mat).tolil()
    lap_mat.setdiag(col_sum)
    return lap_mat.tocsr(), col_sum


def auto_dsygv(M, N, k, k_skip=0, eigen_solver='auto', tol=1E-6,
                  max_iter=100, random_state=None):
    """
    Helper function for solving Generalized Symmetric Eigen Problem
    M * x = a * N * x

    Parameters
    ----------
    M : {array, matrix, sparse matrix, LinearOperator}
        Left hand input matrix: should be symmetric positive semi-definite

    N : {array, matrix, sparse matrix, LinearOperator}
        Right hand input matrix: should be symmetric positive semi-definite

    k : integer
        Number of eigenvalues/vectors to return

    k_skip : integer, optional
        Number of low eigenvalues to skip.

    eigen_solver : string, {'auto', 'arpack', 'dense'}
        auto : algorithm will attempt to choose the best method for input data
        arpack : use arnoldi iteration in shift-invert mode.
                    For this method, M may be a dense matrix, sparse matrix,
                    or general linear operator.
                    Warning: ARPACK can be unstable for some problems.  It is
                    best to try several random seeds in order to check results.
        dense  : use standard dense matrix operations for the eigenvalue
                    decomposition.  For this method, M must be an array
                    or matrix type.  This method should be avoided for
                    large problems.

    tol : float, optional
        Tolerance for 'arpack' method.
        Not used if eigen_solver=='dense'.

    max_iter : maximum number of iterations for 'arpack' method
        not used if eigen_solver=='dense'

    random_state: numpy.RandomState or int, optional
        The generator or seed used to determine the starting vector for arpack
        iterations.  Defaults to numpy.random.

    Returns
    -------
    eigen vectors : ndarray, shape(n_features, k)
    eigen values : ndarray, shape(k)

    """
    if eigen_solver == 'auto':
        if M.shape[0] > 200 and k + k_skip < 10:
            eigen_solver = 'arpack'
        else:
            eigen_solver = 'dense'

    if eigen_solver == 'arpack':
        random_state = check_random_state(random_state)
        v0 = random_state.rand(M.shape[0])
        try:
            eigen_values, eigen_vectors = eigsh(M, k + k_skip, N, sigma=0.0,
                                                tol=tol, maxiter=max_iter,
                                                v0=v0)
        except RuntimeError as msg:
            raise ValueError("Error in solving eigen problem with ARPACK. "
                             "Error message: '%s'. "
                             "Note that method='arpack' can fail when the "
                             "weight matrix is singular or otherwise "
                             "ill-behaved.  method='dense' is recommended. "
                             "See online documentation for more information."
                             % msg)

        return eigen_vectors[:, k_skip:], eigen_values[k_skip:]
    elif eigen_solver == 'dense':
        if hasattr(M, 'toarray'):
            M = M.toarray()
        if hasattr(N, 'toarray'):
            N = N.toarray()
        eigen_values, eigen_vectors = eigh(
            M, N, eigvals=(k_skip, k + k_skip - 1), overwrite_a=True)

#        index = np.argsort(np.abs(eigen_values))
#        return eigen_vectors[:, index], eigen_values
        return eigen_vectors, eigen_values
    else:
        raise ValueError("Unrecognized eigen_solver '%s'" % eigen_solver)


def locality_preserving_projection(X, n_neighbors, mode="distance",
        kernel_func="heat", kernel_param="auto", k=2, eigen_solver='auto',
        tol=1E-6, max_iter=100, random_state=None):
    """Perform Locality Linear Projection

    Parameters
    ----------
    X : array-like, shape: (n_samples, n_features)
        Input data, each column is an edge of a graph

    n_neighbors : int
        number of neighbors to define whether two edges are connected or not

    mode : string
        mode of kneighbors_graph calculation

    kernel_func : string or callable
        kernel function to compute affinity matrix

    kernel_param : float
        parameter for heat kernel

    k : integer
        Number of eigenvalues/vectors to return

    eigen_solver : string, {'auto', 'arpack', 'dense'}
        auto : algorithm will attempt to choose the best method for input data
        arpack : use arnoldi iteration in shift-invert mode.
                    For this method, M may be a dense matrix, sparse matrix,
                    or general linear operator.
                    Warning: ARPACK can be unstable for some problems.  It is
                    best to try several random seeds in order to check results.
        dense  : use standard dense matrix operations for the eigenvalue
                    decomposition.  For this method, M must be an array
                    or matrix type.  This method should be avoided for
                    large problems.

    tol : float, optional
        Tolerance for 'arpack' method.
        Not used if eigen_solver=='dense'.

    max_iter : maximum number of iterations for 'arpack' method
        not used if eigen_solver=='dense'

    random_state: numpy.RandomState or int, optional
        The generator or seed used to determine the starting vector for arpack
        iterations.  Defaults to numpy.random.

    Returns
    -------
    eigen vectors : ndarray, shape(n_features, k)
    eigen values : ndarray, shape(k)
    """

    # fix input parameters    
    if kernel_func == "heat" and kernel_param is None:
        kernel_param = "auto"
    if n_neighbors is None:
        n_neighbors = max(int(X.shape[0] / 10), 1)
    
    # making adjacency matrix
    W = adjacency_matrix(X, n_neighbors, mode)
    # making affinity matrix
    W = affinity_matrix(W, kernel_func, kernel_param)
    # making laplacian matrix
    L, D = laplacian_matrix(W)
    L = safe_sparse_dot(X.T, safe_sparse_dot(L, X))
    D = np.dot(X.T, D[:, np.newaxis] * X)
    return auto_dsygv(L, D, k, 0, eigen_solver, tol, max_iter, random_state)


class LocalityPreservingProjection(BaseEstimator, TransformerMixin):
    """Locality Preserving Projection (LPP) find the optimal linear embedding
    for Graph Laplacian Matrix. LPP can be considered as a linear approximation
    to the Laplacian Eigen Mapping (LEM). While lower dimensional mapping of
    LEM can only be defined to training data set, LPP can also project test
    data set to the lower dimensional space.

    Parameters
    ----------
    n_neighbors : int
        number of neighbors to define whether two edges are connected or not

    mode : string
        mode of kneighbors_graph calculation

    kernel_func : string or callable
        kernel function to compute affinity matrix

    kernel_param : float
        parameter for heat kernel

    k : integer
        Number of eigenvalues/vectors to return

    eigen_solver : string, {'auto', 'arpack', 'dense'}
        auto : algorithm will attempt to choose the best method for input data
        arpack : use arnoldi iteration in shift-invert mode.
                    For this method, M may be a dense matrix, sparse matrix,
                    or general linear operator.
                    Warning: ARPACK can be unstable for some problems.  It is
                    best to try several random seeds in order to check results.
        dense  : use standard dense matrix operations for the eigenvalue
                    decomposition.  For this method, M must be an array
                    or matrix type.  This method should be avoided for
                    large problems.

    tol : float, optional
        Tolerance for 'arpack' method.
        Not used if eigen_solver=='dense'.

    max_iter : maximum number of iterations for 'arpack' method
        not used if eigen_solver=='dense'

    random_state: numpy.RandomState or int, optional
        The generator or seed used to determine the starting vector for arpack
        iterations.  Defaults to numpy.random.

    use_ext : bool
        whether use cython extension or not for constructing adjacency matrix

    pca_preprocess : int, float 
        n_components parameter of PCA class for preprocessing to reduce
        dimension in advance to avoid singularity.
        If `None` is passed, preprocessing will be passed.  
    """

    def __init__(self, n_neighbors=None, n_components=2, mode="distance",
                 kernel_func="heat", kernel_param="auto", eigen_solver='auto',
                 tol=1E-6, max_iter=100, random_state=None,
                 pca_preprocess=0.9):
        self.n_neighbors = n_neighbors
        self.n_components = n_components
        self.mode = mode
        self.kernel_func = kernel_func
        self.kernel_param = kernel_param
        self.eigen_solver = eigen_solver
        self.tol = tol
        self.max_iter = max_iter
        self.random_state = random_state
        self.pca_preprocess = pca_preprocess

    def fit(self, X, y=None):
        assert_all_finite(X)
        X = check_arrays(X, sparse_format='dense')[0]
        n_samples, n_features = X.shape

        did_pca_preprocess = False
        if self.pca_preprocess:
            # PCA preprocess for removing singularity
            # check preprocess param
            if isinstance(self.pca_preprocess, float)\
                    and (0.0 < self.pca_preprocess < 1):
                pca_dim = self.pca_preprocess
            else:
                pca_dim = 0.9
            if int(pca_dim * n_features) > self.n_components:
                _pca = PCA(n_components=pca_dim)
                X = _pca.fit_transform(X)
                did_pca_preprocess = True

        self.components_, _ = locality_preserving_projection(X,
                                  self.n_neighbors, self.mode,
                                  self.kernel_func, self.kernel_param,
                                  self.n_components, self.eigen_solver,
                                  self.tol, self.max_iter,
                                  self.random_state)

        if did_pca_preprocess:
            self.components_ = safe_sparse_dot(_pca.components_.T,
                                               self.components_)

        self.components_ = self.components_.T


        return self

    def transform(self, X):
        assert_all_finite(X)
        return safe_sparse_dot(X, self.components_.T)