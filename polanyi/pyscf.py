"""PySCF geometry optimization interface."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
import functools
from io import StringIO
import os
from os import PathLike
from pathlib import Path
import tempfile
from tempfile import TemporaryDirectory
import shutil
from typing import Any, Optional, Union

import geometric
from geometric.engine import ConicalIntersection
import numpy as np
from pyscf import __config__, lib
from pyscf.geomopt import as_pyscf_method, berny_solver, geometric_solver
from pyscf.grad.rhf import GradientsMixin
from pyscf.gto import Mole

from polanyi import config
from polanyi.data import BOHR_TO_ANGSTROM
from polanyi.evb import evb_eigenvalues
from polanyi.typing import Array2D, ArrayLike2D
from polanyi.utils import convert_elements
from polanyi.xtb import parse_engrad, run_xtb, XTBCalculator


@dataclass
class OptResults:
    """Results of PySCF geometry optimization."""

    coordinates: list[Array2D] = field(default_factory=list)
    energies_diabatic: list[list[float]] = field(default_factory=list)
    energies_adiabatic: list[list[float]] = field(default_factory=list)
    gradients_diabatic: list[list[Array2D]] = field(default_factory=list)
    gradients_adiabatic: list[list[Array2D]] = field(default_factory=list)
    indices: list[list[int]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""

class EnginesWrapper:
    """Wrapper (with list-like behaviours) for multiple engines for ConicalIntersection from geomeTRIC 1.0.1."""
    def __init__(self, engine_list):
        self.engines = engine_list

    def __getitem__(self, key):
        return self.engines[key]

    def __len__(self):
        return len(self.engines)

    def __deepcopy__(self, memo):
        # Create a new wrapper instance, sharing the same engine instances
        # to avoid errors raised by deep copying of non-pickleable parts.
        new_wrapper = EnginesWrapper(self.engines)
        memo[id(self)] = new_wrapper
        return new_wrapper


def e_g_function(
    mol: "Mole",
    topologies: Sequence[bytes],
    results: OptResults,
    keywords: Optional[list[str]] = None,
    xcontrol_keywords: Optional[MutableMapping[str, list[str]]] = None,
    e_shift: float = 0,
    coupling: float = 0,
    path: Optional[Union[str, PathLike]] = None,
) -> tuple[float, Array2D]:
    """Find TS with GFN-FF using xtb command line.
    Args:
        mol: PySCF molecule (coordinates are in Bohr)
        topologies: sequence of GFN-FF topologies for each ground state
        results: OptResults object
        keywords: xtb command line keywords
        xcontrol_keywords: xTB control keywords
        e_shift: energy shift between the ground states
        coupling: coupling constant between the ground states force fields
        path: path where to run calculations
    Returns:
        tuple of adiabatic energy and gradient
    """
    topologies = list(topologies)
    if keywords is None:
        keywords = []
    keywords = set([keyword.strip().lower() for keyword in keywords])
    keywords.add("--grad")
    if path is None:
        path = Path.cwd()
        temp_dirs = [
            TemporaryDirectory(dir=config.TMP_DIR) for i in range(len(topologies))
        ]
        xtb_paths = [path / temp_dir.name for temp_dir in temp_dirs]
        cleanup = True
    else:
        path = Path(path)
        xtb_paths = [path / str(i) for i in range(len(topologies))]
        cleanup = False
    elements = mol.atom_charges()
    # Get coordinates in Angstrom
    coordinates = mol.atom_coords() * BOHR_TO_ANGSTROM

    energies = []
    gradients = []
    for topology, xtb_path in zip(topologies, xtb_paths):
        xtb_path.mkdir(exist_ok=True)
        if not (xtb_path / "gfnff_topo").exists():
            with open(xtb_path / "gfnff_topo", "wb") as f:
                f.write(topology)
        run_xtb(
            elements,
            coordinates,
            path=xtb_path,
            keywords=keywords,
            xcontrol_keywords=xcontrol_keywords,
        )
        energy, gradient = parse_engrad(xtb_path / "xtb.engrad")
        energies.append(energy)
        gradients.append(gradient)

    energies[-1] += e_shift

    # Solve EVB
    energies_ad, gradients_ad, indices = evb_eigenvalues(
        energies, gradients=gradients, coupling=coupling
    )

    # Clean up temporary directory
    if cleanup is True:
        for temp_dir in temp_dirs:
            temp_dir.cleanup()

    # Store results
    results.coordinates.append(coordinates)
    results.energies_diabatic.append(energies)
    results.energies_adiabatic.append(energies_ad)
    results.gradients_diabatic.append(gradients)
    results.gradients_adiabatic.append(gradients_ad)
    results.indices.append(indices)

    return energies_ad[1], gradients_ad[1]


def e_g_function_ci(
    mol: "Mole",
    topologies: Sequence[bytes],
    results: OptResults,
    keywords: Optional[list[str]] = None,
    xcontrol_keywords: Optional[MutableMapping[str, list[str]]] = None,
    e_shift: float = 0,
    path: Optional[Union[str, PathLike]] = None,
) -> tuple[float, Array2D]:
    """Find TS with GFN-FF.
    Args:
        mol: PySCF molecule (coordinates in Bohr)
        topologies: sequence of GFN-FF topologies for each ground state
        results: OptResults object
        e_shift: energy shift between the ground states
        coupling: coupling constant between the ground states force fields
        path: path where to run calculations
    Returns:
        tuple of adiabatic energy and gradient
    """
    topologies = list(topologies)
    if keywords is None:
        keywords = []
    keywords = set([keyword.strip().lower() for keyword in keywords])
    keywords.add("--grad")
    if path is None:
        path = Path.cwd()
        temp_dirs = [
            TemporaryDirectory(dir=config.TMP_DIR) for i in range(len(topologies))
        ]
        xtb_paths = [path / temp_dir.name for temp_dir in temp_dirs]
        cleanup = True
    else:
        path = Path(path)
        xtb_paths = [path / str(i) for i in range(len(topologies))]
        cleanup = False

    elements = mol.atom_charges()
    # Get coordinates in Angstrom
    coordinates = mol.atom_coords() * BOHR_TO_ANGSTROM

    energies = []
    gradients = []
    for topology, xtb_path in zip(topologies, xtb_paths):
        xtb_path.mkdir(exist_ok=True)
        if not (xtb_path / "gfnff_topo").exists():
            with open(xtb_path / "gfnff_topo", "wb") as f:
                f.write(topology)
        run_xtb(
            elements,
            coordinates,
            path=xtb_path,
            keywords=keywords,
            xcontrol_keywords=xcontrol_keywords,
        )
        energy, gradient = parse_engrad(xtb_path / "xtb.engrad")
        energies.append(energy)
        gradients.append(gradient)

    energies[-1] += e_shift

    # Solve EVB
    energies_ad, gradients_ad, indices = evb_eigenvalues(
        energies, gradients=gradients, coupling=coupling
    )

    # Clean up temporary directory
    if cleanup is True:
        for temp_dir in temp_dirs:
            temp_dir.cleanup()

    # Store results
    results.coordinates.append(coordinates)
    results.energies_diabatic.append(energies)
    results.energies_adiabatic.append(energies_ad)
    results.gradients_diabatic.append(gradients)
    results.gradients_adiabatic.append(gradients_ad)
    results.indices.append(indices)

    return energies_ad[1], gradients_ad[1]


def e_g_function_python(
    mol: "Mole",
    calculators: Sequence[XTBCalculator],
    results: OptResults,
    e_shift: float = 0,
    coupling: float = 0,
    path: Optional[Union[str, PathLike]] = None,
) -> tuple[float, Array2D]:
    """Find TS with GFN-FF using xtb-python.
    Args:
        mol: PySCF molecule (coordinates in Bohr)
        calculators: xtb-python calculators
        results: OptResults object
        e_shift: energy shift between the ground states
        coupling: coupling constant between the ground states force fields
        path: path where to run calculations
    Returns:
        tuple of adiabatic energy and gradient
    """
    if path is None:
        path = Path.cwd()
    else:
        path = Path(path)
    # Get coordinates in Angstrom
    coordinates: np.ndarray = np.ascontiguousarray(mol.atom_coords()) * BOHR_TO_ANGSTROM

    energies = []
    gradients = []
    for calculator in calculators:
        calculator.coordinates = coordinates
        energy, gradient = calculator.sp(return_gradient=True)
        energies.append(energy)
        gradients.append(gradient)

    energies[-1] += e_shift

    # Solve EVB
    energies_ad, gradients_ad, indices = evb_eigenvalues(
        energies, gradients=gradients, coupling=coupling
    )

    # Store results
    results.coordinates.append(coordinates)
    results.energies_diabatic.append(energies)
    results.energies_adiabatic.append(energies_ad)
    results.gradients_diabatic.append(gradients)
    results.gradients_adiabatic.append(gradients_ad)
    results.indices.append(indices)

    return energies_ad[1], gradients_ad[1]


def e_g_function_ci_python(
    mol: "Mole",
    calculator: XTBCalculator,
    e_shift: float = 0,
    path: Optional[Union[str, PathLike]] = None,
) -> tuple[float, Array2D]:
    """Find TS with GFN-FF using xtb-python and conical intersection.
    Args:
        mol: PySCF molecule (coordinates are in Bohr)
        calculator: xtb-python calculator
        e_shift: energy shift between the ground states
        path: path where to run calculations
    Returns:
        tuple of energy and gradient
    """
    if path is None:
        path = Path.cwd()
    else:
        path = Path(path)

    # Get coordinates in Angstrom
    coordinates = np.ascontiguousarray(mol.atom_coords()) * BOHR_TO_ANGSTROM

    calculator.coordinates = coordinates
    energy, gradient = calculator.sp(return_gradient=True)
    energy += e_shift

    return energy, gradient


def ts_from_gfnff(
    elements: Union[Sequence[int], Sequence[str]],
    coordinates: ArrayLike2D,
    topologies: Sequence[bytes],
    keywords: Optional[list[str]] = None,
    xcontrol_keywords: Optional[MutableMapping[str, list[str]]] = None,
    e_shift: float = 0,
    coupling: float = 0.001,
    maxsteps: int = 100,
    callback: Optional[Callable[[dict[str, Any]], None]] = None,
    conv_params: Optional[dict[str, Any]] = None,
    solver: str = "geometric",
    path: Optional[Union[str, PathLike]] = None,
) -> OptResults:
    """Optimize TS with GFNFF."""
    if conv_params is None:
        conv_params = {}
    if path:
        path = Path(path)
        path.mkdir(exist_ok=True)
    if keywords is None:
        keywords = []
    keywords = set([keyword.strip().lower() for keyword in keywords])
    keywords.add("--gfnff")
    results = OptResults()

    mole = get_pyscf_mole(elements, coordinates)

    e_g_partial = functools.partial(
        e_g_function,
        topologies=topologies,
        results=results,
        keywords=keywords,
        xcontrol_keywords=xcontrol_keywords,
        e_shift=e_shift,
        coupling=coupling,
        path=path,
    )

    if solver == "pyberny":
        pyscf_solver = berny_solver
    elif solver == "geometric":
        pyscf_solver = geometric_solver
    with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
        pyscf_solver.optimize(
            as_pyscf_method(mole, e_g_partial),
            maxsteps=maxsteps,
            callback=callback,
            **conv_params,
        )
    results.stdout = stdout.getvalue()
    results.stderr = stderr.getvalue()

    return results


def ts_from_gfnff_python(
    elements: Union[Sequence[int], Sequence[str]],
    coordinates: ArrayLike2D,
    calculators: Sequence[XTBCalculator],
    e_shift: float = 0,
    coupling: float = 0.001,
    maxsteps: int = 100,
    callback: Optional[Callable[[dict[str, Any]], None]] = None,
    conv_params: Optional[dict[str, Any]] = None,
    solver: str = "geometric",
    path: Optional[Union[str, PathLike]] = None,
) -> OptResults:
    """Optimize TS with GFNFF."""
    if conv_params is None:
        conv_params = {}
    if path is None:
        path = Path.cwd()
    else:
        path = Path(path)
        path.mkdir(exist_ok=True)
    results = OptResults()

    mole = get_pyscf_mole(elements, coordinates)

    e_g_partial = functools.partial(
        e_g_function_python,
        calculators=calculators,
        results=results,
        e_shift=e_shift,
        coupling=coupling,
        path=path,
    )

    if solver == "pyberny":
        pyscf_solver = berny_solver
    elif solver == "geometric":
        pyscf_solver = geometric_solver
    with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
        pyscf_solver.optimize(
            as_pyscf_method(mole, e_g_partial),
            maxsteps=maxsteps,
            callback=callback,
            **conv_params,
        )

    results.stdout = stdout.getvalue()
    results.stderr = stderr.getvalue()

    return results


def ts_from_gfnff_ci(
    elements: Union[Sequence[int], Sequence[str]],
    coordinates: ArrayLike2D,
    topologies: Sequence[bytes],
    keywords: Optional[list[str]] = None,
    xcontrol_keywords: Optional[MutableMapping[str, list[str]]] = None,
    e_shift: float = 0,
    coupling: float = 0.001,
    maxsteps: int = 100,
    callback: Optional[Callable[[dict[str, Any]], None]] = None,
    conv_params: Optional[dict[str, Any]] = None,
    solver: str = "geometric",
    path: Optional[Union[str, PathLike]] = None,
) -> OptResults:
    """Optimize TS with GFNFF."""
    if conv_params is None:
        conv_params = {}
    if path:
        path = Path(path)
        path.mkdir(exist_ok=True, parents=True)
    if keywords is None:
        keywords = []
    keywords = set([keyword.strip().lower() for keyword in keywords])
    keywords.add("--gfnff")
    results = OptResults()

    mole = get_pyscf_mole(elements, coordinates)

    e_g_partial = functools.partial(
        e_g_function,
        topologies=topologies,
        results=results,
        keywords=keywords,
        xcontrol_keywords=xcontrol_keywords,
        e_shift=e_shift,
        coupling=coupling,
        path=path,
    )

    if solver == "pyberny":
        pyscf_solver = berny_solver
    elif solver == "geometric":
        pyscf_solver = geometric_solver
    with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
        pyscf_solver.optimize(
            as_pyscf_method(mole, e_g_partial),
            maxsteps=maxsteps,
            callback=callback,
            **conv_params,
        )
    results.stdout = stdout.getvalue()
    results.stderr = stderr.getvalue()

    return results


def ts_from_gfnff_ci_python(
    elements: Union[Sequence[int], Sequence[str]],
    coordinates: ArrayLike2D,
    calculators: Sequence[XTBCalculator],
    e_shift: float = 0,
    maxsteps: int = 100,
    alpha: float = 0.025,
    sigma: float = 3.5,
    callback: Optional[Callable[[dict[str, Any]], None]] = None,
    conv_params: Optional[dict[str, Any]] = None,
    path: Optional[Union[str, PathLike]] = None,
) -> Array2D:
    """Optimize TS with GFNFF."""
    if conv_params is None:
        conv_params = {}
    if path is None:
        path = Path.cwd()
    else:
        path = Path(path)
        path.mkdir(exist_ok=True, parents=True)

    mole = get_pyscf_mole(elements, coordinates)

    e_g_partial_1 = functools.partial(
        e_g_function_ci_python,
        calculator=calculators[0],
        e_shift=0,
        path=path,
    )
    e_g_partial_2 = functools.partial(
        e_g_function_ci_python,
        calculator=calculators[1],
        e_shift=e_shift,
        path=path,
    )

    _, opt_mole = optimize_ci(
        [as_pyscf_method(mole, e_g_partial_1), as_pyscf_method(mole, e_g_partial_2)],
        maxsteps=maxsteps,
        alpha=alpha,
        sigma=sigma,
        callback=callback,
        **conv_params,
    )

    opt_coordinates: Array2D = (
        np.ascontiguousarray(opt_mole.atom_coords()) * BOHR_TO_ANGSTROM
    )

    return opt_coordinates


def get_pyscf_mole(
    elements: Union[Sequence[int], Sequence[str]],
    coordinates: ArrayLike2D,
) -> "Mole":
    """Return PySCF atom list."""
    elements = convert_elements(elements, output="symbols")
    coordinates = np.array(coordinates)
    atoms = []
    for element, coord in zip(elements, coordinates):
        atoms.append((element, tuple(coord)))

    numbers = convert_elements(elements, output="numbers")
    n_electrons = sum(numbers)
    mole = Mole(verbose=0, basis="def2svp")
    mole.spin = n_electrons % 2
    mole.atom = atoms
    mole.build()

    return mole


INCLUDE_GHOST: bool = getattr(
    __config__, "geomopt_berny_solver_optimize_include_ghost", True
)
ASSERT_CONV: bool = getattr(
    __config__, "geomopt_berny_solver_optimize_assert_convergence", True
)


def optimize_ci(
    methods: list[Any],
    assert_convergence: bool = ASSERT_CONV,
    include_ghost: bool = INCLUDE_GHOST,
    constraints: Any = None,
    callback: Any = None,
    maxsteps: int = 100,
    alpha: float = 0.025,
    sigma: float = 3.5,
    **kwargs,
) -> tuple[bool, "Mole"]:
    """Modified PySCF code to run geomeTRIC with CI optimization."""
    g_scanners = []
    for method in methods:
        if isinstance(method, lib.GradScanner):
            g_scanner = method
        elif isinstance(method, GradientsMixin):
            g_scanner = method.as_scanner()
        elif getattr(method, "nuc_grad_method", None):
            g_scanner = method.nuc_grad_method().as_scanner()
        else:
            raise NotImplementedError("Nuclear gradients of %s not available" % method)
        if not include_ghost:
            g_scanner.atmlst = np.where(method.mol.atom_charges() != 0)[0]
        g_scanners.append(g_scanner)

    tmpf = tempfile.mktemp(dir=lib.param.TMPDIR)
    engine_1 = geometric_solver.PySCFEngine(g_scanners[0])
    engine_2 = geometric_solver.PySCFEngine(g_scanners[1])
    M = engine_1.M
    meci_sigma = sigma
    meci_alpha = alpha
    engine_1.callback = callback
    engines_wrapper = EnginesWrapper([engine_1, engine_2])
    engine = ConicalIntersection(M, engines_wrapper , meci_sigma, meci_alpha)
    engine.maxsteps = maxsteps
    # To avoid overwritting method.mol
    engine.mol = g_scanners[0].mol.copy()

    # When symmetry is enabled, the molecule may be shifted or rotated to make
    # the z-axis be the main axis. The transformation can cause inconsistency
    # between the optimization steps. The transformation is muted by setting
    # an explict point group to the keyword mol.symmetry (see symmetry
    # detection code in Mole.build function).
    if engine.mol.symmetry:
        engine.mol.symmetry = engine.mol.topgroup

    # Provide config file log.ini for geomeTRIC optimisation
    kwargs["logIni"] = os.path.abspath(os.path.abspath(os.path.join(__file__, "..", "log.ini")))

    engine.assert_convergence = assert_convergence
    try:
        geometric.optimize.run_optimizer(
            customengine=engine, input=tmpf, constraints=constraints, **kwargs
        )
        conv = True
        # method.mol.set_geom_(m.xyzs[-1], unit='Angstrom')
    except geometric_solver.NotConvergedError as e:
        lib.logger.note(method, str(e))
        conv = False
    
    # Remove the temporary files created by geomeTRIC
    if os.path.exists(f"{tmpf}_optim.xyz"):
        os.remove(f"{tmpf}_optim.xyz")
    if os.path.exists(f"{tmpf}.tmp"):
        shutil.rmtree(f"{tmpf}.tmp")

    return conv, engine.mol
