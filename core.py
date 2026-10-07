"""PySCF coupled-cluster reference energies for DFThub archives.

This module deliberately keeps PySCF imports inside worker functions.  API
processes can therefore import the package on hosts where the optional PySCF
environment is not installed, while the configured calculation interpreter
still performs the actual coupled-cluster calculation.

``compute_reference`` accepts a DFThub ``.tgz``/``.tar.gz``/``.zip`` archive
containing XYZ files and a reference template.  The template may either be a
normal DFThub reference file or contain only the coefficient/name columns;
calculated values are inserted in the reference column and the original
columns/order are retained.  A completely empty template produces one
``1 <name> <energy> 1`` line per molecule.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import subprocess
import math
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

HARTREE_TO_KCAL = 627.509
METHODS = ("CCSD", "CCSD(T)", "CCSDT", "CCSDT(Q)")

# Public aliases used by the front end.  The canonical PySCF names are
# explicitly exposed so callers can show exactly what a preset means.
BASIS_ALIASES = {
    "3zeta": "cc-pvtz",
    "3z": "cc-pvtz",
    "tz": "cc-pvtz",
    "triple-zeta": "cc-pvtz",
    "triple_zeta": "cc-pvtz",
    "cc-pvtz": "cc-pvtz",
    "4zeta": "cc-pvqz",
    "4z": "cc-pvqz",
    "qz": "cc-pvqz",
    "quadruple-zeta": "cc-pvqz",
    "quadruple_zeta": "cc-pvqz",
    "cc-pvqz": "cc-pvqz",
    "5zeta": "cc-pv5z",
    "5z": "cc-pv5z",
    "cc-pv5z": "cc-pv5z",
    "aug-cc-pvtz": "aug-cc-pvtz",
    "aug-cc-pvqz": "aug-cc-pvqz",
    "aug-cc-pv5z": "aug-cc-pv5z",
    "cbs": "CBS",
    "complete-basis-set": "CBS",
    "complete_basis_set": "CBS",
}


class CalculationCancelled(RuntimeError):
    """Raised when a running calculation is asked to terminate."""


class CalculationError(RuntimeError):
    """Raised for malformed inputs or an unavailable PySCF method."""


@dataclass(frozen=True)
class CalculationSettings:
    """Resource and method settings for one reference calculation.

    ``pool_size`` is the number of molecules evaluated concurrently,
    ``memory_mb`` is passed to PySCF's SCF/CC objects, and ``task_threads`` is
    the BLAS/PySCF thread count for each molecule.
    """

    method: str = "CCSD(T)"
    basis: str = "3zeta"
    pool_size: int = 1
    memory_mb: int = 4096
    task_threads: int = 1
    cbs_exponent: float = 3.0
    memory_pool_mb: int | None = None
    basis_family: str = "cc"
    cbs_pair: str = "34"

    def __post_init__(self) -> None:
        method = _canonical_method(self.method)
        basis = _canonical_basis(self.basis)
        family = str(self.basis_family).lower()
        if family not in {"cc", "aug"}:
            raise ValueError("basis_family must be cc or aug")
        if basis.startswith("aug-"):
            family = "aug"
        elif family == "aug" and basis != "CBS":
            basis = "aug-" + basis
        pair = str(self.cbs_pair)
        if pair not in {"34", "45"}:
            raise ValueError("cbs_pair must be 34 or 45")
        object.__setattr__(self, "basis_family", family)
        object.__setattr__(self, "cbs_pair", pair)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "basis", basis)
        for name in ("pool_size", "memory_mb", "task_threads"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or int(value) < 1:
                raise ValueError(f"{name} must be a positive integer")
            object.__setattr__(self, name, int(value))
        total_memory = self.memory_pool_mb
        if total_memory is None:
            total_memory = self.memory_mb * self.pool_size
        if isinstance(total_memory, bool) or int(total_memory) != total_memory or total_memory < self.pool_size:
            raise ValueError("memory_pool_mb must be an integer at least pool_size")
        object.__setattr__(self, "memory_pool_mb", int(total_memory))
        object.__setattr__(self, "memory_mb", min(self.memory_mb, int(total_memory) // self.pool_size))
        if not math.isfinite(float(self.cbs_exponent)) or float(self.cbs_exponent) <= 0:
            raise ValueError("cbs_exponent must be positive")


@dataclass(frozen=True)
class Molecule:
    name: str
    path: Path
    charge: int
    multiplicity: int
    atom_block: str
    natom: int


@dataclass(frozen=True)
class MoleculeResult:
    name: str
    energy_hartree: float | None
    basis: str
    method: str
    elapsed_s: float = 0.0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "energy_hartree": self.energy_hartree,
            "basis": self.basis,
            "method": self.method,
            "elapsed_s": round(self.elapsed_s, 6),
            "details": self.details,
            "status": "ok" if self.error is None and self.energy_hartree is not None else "failed",
            **({"error": self.error} if self.error else {}),
        }


@dataclass
class CalculationReport:
    output_path: Path
    method: str
    basis: str
    results: list[MoleculeResult] = field(default_factory=list)
    cancelled: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    reactions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.results) and not self.cancelled and all(
            r.energy_hartree is not None and r.error is None for r in self.results
        )

    @property
    def failed(self) -> list[MoleculeResult]:
        return [r for r in self.results if r.error or r.energy_hartree is None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "cancelled": self.cancelled,
            "method": self.method,
            "basis": self.basis,
            "output": str(self.output_path),
            "settings": self.settings,
            "reactions": self.reactions,
            "molecularEnergies": [result.to_dict() for result in self.results],
            "failed": [result.to_dict() for result in self.failed],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class _RefLine:
    """A validated reference row or a comment/blank line.

    The first six fields remain positional for the empty-template rows built
    by :class:`ReferenceCalculator`. ``tokens`` retain the original pair and
    ratio spelling; numerical references are replaced when rendering.
    """

    original: str
    tokens: tuple[str, ...]
    pairs: tuple[tuple[float, str], ...]
    trailing: tuple[str, ...]
    ref_value: float | None
    ratio: float | None
    comment: str = ""
    passthrough: bool = False


def _canonical_method(method: str) -> str:
    if not isinstance(method, str):
        raise ValueError(f"Unsupported coupled-cluster method: {method!r}")
    cleaned = method.strip().upper().replace("−", "-")
    aliases = {"CCSD(T)": "CCSD(T)"}
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in METHODS:
        raise ValueError(f"Unsupported coupled-cluster method {method!r}; choose one of {METHODS}")
    return cleaned


def _canonical_basis(basis: str) -> str:
    if not isinstance(basis, str):
        raise ValueError(f"Unsupported basis preset: {basis!r}")
    cleaned = basis.strip().lower().replace(" ", "")
    try:
        return BASIS_ALIASES[cleaned]
    except KeyError as exc:
        raise ValueError("Unsupported basis preset; choose 3zeta, 4zeta, 5zeta, or CBS") from exc


def _is_float(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _parse_ref_line(line: str, molecule_names: set[str] | None = None) -> _RefLine | None:
    """Parse one row according to the grammar documented in ``parse_reference``."""

    original = line.rstrip("\r\n")
    if not original.strip() or original.lstrip().startswith("#"):
        return _RefLine(original, (), (), (), None, None, passthrough=True)

    code, separator, comment_text = original.partition("#")
    comment = (separator + comment_text).rstrip() if separator else ""
    # One empty tab cell denotes an explicit missing reference. Two or more
    # are legacy column padding; use ? when a padded template has no value.
    code = code.lstrip(" \t").rstrip(" \r\n")
    tokens = tuple(code.split())
    placeholders = {"?", "na", "null"}

    def finite_number(value: str, field_name: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise CalculationError(f"{field_name} must be a number, got {value!r}") from exc
        if not math.isfinite(parsed):
            raise CalculationError(f"{field_name} must be finite, got {value!r}")
        return parsed

    explicit_blank = False
    pair_tokens: tuple[str, ...]
    trailing: tuple[str, ...]
    if "\t" in code and sum(not cell.strip() for cell in code.split("\t")) == 1:
        cells = code.split("\t")
        first_empty = next(index for index, cell in enumerate(cells) if not cell.strip())
        pair_tokens = tuple(" ".join(cells[:first_empty]).split())
        suffix = tuple(" ".join(cells[first_empty:]).split())
        if len(pair_tokens) < 2 or len(pair_tokens) % 2 or len(suffix) > 1:
            raise CalculationError("An empty tab field is only allowed for the reference value after coefficient/name pairs")
        explicit_blank = True
        trailing = ("",) + suffix
    elif len(tokens) < 2:
        raise CalculationError("A reference row requires at least one coefficient/name pair")
    elif len(tokens) % 2:
        # 2n + 1 fields: a Hartree reference, including a placeholder.
        pair_tokens, trailing = tokens[:-1], tokens[-1:]
    elif len(tokens) == 2:
        pair_tokens, trailing = tokens, ()
    elif not _is_float(tokens[-1]) and tokens[-1].lower() not in placeholders:
        # Omitted reference after ordinary names, e.g. ``1 A -1 B``.
        pair_tokens, trailing = tokens, ()
    else:
        # 2n + 2 fields: the reference is in kcal/mol and the final field is
        # the downstream normalization ratio. Numeric names are permitted in
        # the pairs; use an explicit placeholder to disambiguate an omitted
        # reference after a numeric name.
        pair_tokens, trailing = tokens[:-2], tokens[-2:]

    if not pair_tokens or len(pair_tokens) % 2:
        raise CalculationError("Coefficient/name pairs are incomplete")
    pairs: list[tuple[float, str]] = []
    for index in range(0, len(pair_tokens), 2):
        coefficient = finite_number(pair_tokens[index], "Coefficient")
        name = pair_tokens[index + 1]
        if molecule_names is not None and name not in molecule_names:
            raise CalculationError(f"Reference molecule not found in archive: {name}")
        pairs.append((coefficient, name))

    ref_value = None
    if trailing and not explicit_blank and trailing[0].lower() not in placeholders:
        ref_value = finite_number(trailing[0], "Reference value")
    ratio = None
    if len(trailing) == 2:
        ratio = finite_number(trailing[1], "Ratio")
        if ratio <= 0:
            raise CalculationError("Ratio must be greater than zero")
    return _RefLine(original, pair_tokens + trailing, tuple(pairs), trailing, ref_value, ratio, comment)


def parse_reference(text: str, molecule_names: Iterable[str] | None = None) -> list[_RefLine]:
    """Validate a DFThub reference template, preserving rows and comments.

    Each row starts with one or more ``coefficient molecule`` pairs. A name
    may be numeric (``1 123 ? 1`` references ``123.xyz``); ``P1`` is also a
    molecule name, never a reaction label. The permitted endings are:

    * ``1 A -1 B -0.2``: an existing Hartree value (2n + 1 tokens).
    * ``1 A -1 B -125.5 2``: kcal/mol value, then a positive normalization
      ratio (2n + 2 tokens). Rendering does not divide the value by the ratio.
    * ``1 A -1 B ?`` or ``... NA`` or ``... null``: blank Hartree value.
    * ``1 A -1 B ? 2``: blank kcal/mol value with ratio 2.
    * ``1 A -1 B``: omitted Hartree value. After a numeric molecule name,
      append ``?`` to disambiguate this form from a value/ratio ending.
    * ``1\\tA\\t-1\\tB\\t\\t2``: an explicit empty tab field followed by ratio
      2, hence kcal/mol. A trailing empty tab field alone means Hartree.

    Two or more empty tab cells are legacy padding and are treated as whitespace.
    Use ``?`` for a missing value when the template already has padding.
    A single numeric tail, even ``1``, is an existing Hartree reference.
    Blank lines and ``#`` comments are preserved when there are data rows.
    Empty/comment-only templates return an empty list so the calculator can
    generate its usual one-row-per-molecule result. Malformed rows, unknown
    names (when ``molecule_names`` is supplied), non-finite numbers and
    non-positive ratios raise :class:`CalculationError` with a line number.
    """

    names = set(molecule_names) if molecule_names is not None else None
    parsed_lines: list[_RefLine] = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            parsed = _parse_ref_line(line, names)
        except CalculationError as exc:
            raise CalculationError(f"Reference line {number}: {exc}") from exc
        if parsed is not None:
            parsed_lines.append(parsed)
    return parsed_lines if any(line.pairs for line in parsed_lines) else []


def _render_reference(lines: Sequence[_RefLine], energies: Mapping[str, float]) -> str:
    rendered: list[str] = []
    for line in lines:
        if line.passthrough:
            rendered.append(line.original)
            continue
        missing = [name for _, name in line.pairs if name not in energies]
        if missing:
            raise CalculationError(
                f"Reference line refers to molecule(s) not found in archive: {', '.join(missing)}"
            )
        terms = []
        for coefficient, name in line.pairs:
            try:
                energy = float(energies[name])
            except (TypeError, ValueError) as exc:
                raise CalculationError(f"Molecular energy must be numeric for {name}") from exc
            if not math.isfinite(energy) or not math.isfinite(coefficient):
                raise CalculationError(f"Molecular energy and coefficient must be finite for {name}")
            term = coefficient * energy
            if not math.isfinite(term):
                raise CalculationError(f"Reaction energy overflow for {name}")
            terms.append(term)
        try:
            value_hartree = math.fsum(terms)
        except OverflowError as exc:
            raise CalculationError("Reaction energy overflow") from exc
        if line.ratio is not None and (not math.isfinite(line.ratio) or line.ratio <= 0):
            raise CalculationError("Ratio must be finite and greater than zero")
        value = value_hartree * HARTREE_TO_KCAL if line.ratio is not None else value_hartree
        if not math.isfinite(value):
            raise CalculationError("Calculated reference value must be finite")
        prefix = list(line.tokens[: 2 * len(line.pairs)])
        prefix.append(f"{value:.12f}")
        if line.ratio is not None:
            prefix.append(line.trailing[-1] if len(line.trailing) == 2 else f"{line.ratio:g}")
        rendered.append(" ".join(prefix) + (" " + line.comment if line.comment else ""))
    return "\n".join(rendered) + ("\n" if rendered else "")


def _safe_extract(archive: Path, destination: Path) -> None:
    """Read regular archive members with bounded expansion and no links."""
    destination = destination.resolve()
    seen = set()
    total = 0
    def target_for(name, size):
        nonlocal total
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise CalculationError(f"Unsafe archive member: {name}")
        target = (destination / relative).resolve()
        if destination not in target.parents or target in seen:
            raise CalculationError(f"Unsafe or duplicate archive member: {name}")
        seen.add(target)
        total += size
        if size > 8 * 1024**2 or total > 512 * 1024**2 or len(seen) > 10000:
            raise CalculationError("Archive exceeds limits: 8 MiB/member, 512 MiB expanded, 10000 files")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                if member.is_dir():
                    continue
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise CalculationError("Archive links are not allowed")
                target = target_for(member.filename, member.file_size)
                with handle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, mode="r:gz") as handle:
            for member in handle:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise CalculationError(f"Archive links and special members are not allowed: {member.name}")
                target = target_for(member.name, member.size)
                source = handle.extractfile(member)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def _read_xyz(path: Path) -> Molecule:
    try:
        lines = path.read_text(errors="strict").splitlines()
    except OSError as exc:
        raise CalculationError(f"Cannot read XYZ file {path}: {exc}") from exc
    if not lines:
        raise CalculationError(f"XYZ file is empty: {path.name}")
    try:
        natom = int(lines[0].split()[0])
    except (IndexError, ValueError) as exc:
        raise CalculationError(f"Invalid atom count in {path.name}") from exc
    if natom < 1 or len(lines) < natom + 2:
        raise CalculationError(f"XYZ file {path.name} has fewer than {natom} atoms")
    comment = lines[1].split()
    try:
        charge, multiplicity = int(comment[0]), int(comment[1])
    except (IndexError, ValueError) as exc:
        raise CalculationError(f"XYZ second line must contain charge and multiplicity: {path.name}") from exc
    if any(line.strip() for line in lines[natom + 2:]):
        raise CalculationError(f"Extra atom/geometry data in {path.name}")
    if multiplicity < 1:
        raise CalculationError(f"Invalid spin multiplicity in {path.name}")
    atom_block = "\n".join(lines[2 : 2 + natom])
    for atom_line in atom_block.splitlines():
        fields = atom_line.split()
        if len(fields) < 4:
            raise CalculationError(f"Invalid atom line in {path.name}: {atom_line!r}")
        try:
            coordinates = [float(item) for item in fields[1:4]]
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError("Non-finite coordinate")
        except ValueError as exc:
            raise CalculationError(f"Invalid coordinates in {path.name}: {atom_line!r}") from exc
    name = path.stem
    return Molecule(name, path, charge, multiplicity, atom_block, natom)


def _cancelled(cancel_event: Any) -> bool:
    if cancel_event is None:
        return False
    if isinstance(cancel_event, (str, Path)):
        return Path(cancel_event).exists()
    checker = getattr(cancel_event, "is_set", None)
    if callable(checker):
        return bool(checker())
    if callable(cancel_event):
        return bool(cancel_event())
    return bool(cancel_event)


@contextlib.contextmanager
def _thread_environment(threads: int):
    names = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = str(threads)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _basis_for(atom_block: str, basis_name: str):
    """Load the requested all-electron basis directly from PySCF.

    A missing element is an input error. Never replace a basis silently or
    supply an unmatched pseudopotential for an all-electron calculation.
    """

    from pyscf import gto
    from pyscf.data.elements import _std_symbol

    basis = {}
    for row in atom_block.splitlines():
        if not row.strip():
            continue
        symbol = _std_symbol(row.split()[0])
        if symbol in basis:
            continue
        try:
            functions = gto.basis.load(basis_name, symbol)
            if not functions:
                raise ValueError("empty basis")
            if gto.basis.load_ecp(basis_name, symbol):
                raise ValueError("basis requires an ECP; this preset is all-electron")
        except Exception as exc:
            raise CalculationError(f"Cannot use {basis_name} for {symbol}: {exc}") from exc
        basis[symbol] = functions
    return basis, None


def _finite_energy(value: Any, label: str, molecule_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CalculationError(f"PySCF returned invalid {label} for {molecule_name}") from exc
    if not math.isfinite(result):
        raise CalculationError(f"PySCF returned non-finite {label} for {molecule_name}")
    return result


def _cc_energy(molecule: Molecule, method: str, basis_name: str, memory_mb: int, threads: int, cancel_event: Any) -> dict[str, Any]:
    """Run exactly the requested method and return auditable energy parts."""

    if _cancelled(cancel_event):
        raise CalculationCancelled()
    if method not in METHODS:
        raise CalculationError(f"Unsupported method: {method}")
    reference_type = "RHF" if molecule.multiplicity == 1 else "UHF"
    if method == "CCSDT(Q)":
        from .capabilities import get_capabilities

        capability = next(row for row in get_capabilities()["methods"] if row["name"] == method)
        supported = capability["closed_shell" if reference_type == "RHF" else "open_shell"]
        if not supported:
            raise CalculationError(capability["reason"] or f"CCSDT(Q) is unavailable for {reference_type} in this PySCF installation")
    with _thread_environment(threads):
        import pyscf
        from pyscf import cc, gto, lib, scf

        lib.num_threads(threads)
        basis, ecp = _basis_for(molecule.atom_block, basis_name)
        mol_kwargs = {
            "atom": molecule.atom_block,
            "basis": basis,
            "charge": molecule.charge,
            "spin": molecule.multiplicity - 1,
            "verbose": 0,
            "max_memory": memory_mb,
        }
        if ecp is not None:
            mol_kwargs["ecp"] = ecp
        mol = gto.M(**mol_kwargs)
        mf = scf.RHF(mol) if molecule.multiplicity == 1 else scf.UHF(mol)
        mf.max_memory = memory_mb
        mf.conv_tol = 1e-10
        mf.max_cycle = 100

        def stop_scf(envs):
            if _cancelled(cancel_event):
                raise CalculationCancelled()

        mf.callback = stop_scf
        mf.kernel()
        if _cancelled(cancel_event):
            raise CalculationCancelled()
        if not mf.converged:
            raise CalculationError(f"SCF did not converge for {molecule.name}")
        hf_energy = _finite_energy(mf.e_tot, "HF energy", molecule.name)
        factory_name = "CCSD" if method in {"CCSD", "CCSD(T)"} else "CCSDT"
        factory = getattr(cc, factory_name, None)
        if not callable(factory):
            raise CalculationError(f"Installed PySCF does not implement {factory_name}")
        runner = factory(mf)
        runner.max_memory = memory_mb
        runner.conv_tol = 1e-9
        runner.conv_tol_normt = 1e-7
        runner.max_cycle = 100
        runner.callback = stop_scf
        runner.kernel()
        if _cancelled(cancel_event):
            raise CalculationCancelled()
        if not getattr(runner, "converged", False):
            raise CalculationError(f"{factory_name} did not converge for {molecule.name}")
        correlation = _finite_energy(getattr(runner, "e_corr", None), f"{factory_name} correlation energy", molecule.name)
        correction = 0.0
        if method == "CCSD(T)":
            correction = _finite_energy(runner.ccsd_t(), "(T) correction", molecule.name)
        elif method == "CCSDT(Q)":
            # PySCF 2.14 returns ([Q], (Q)). The second element is already
            # the full (Q) correction; adding both would double-count [Q].
            try:
                quadruples = runner.ccsdt_q(runner.tamps)
            except (AttributeError, NotImplementedError) as exc:
                raise CalculationError("Installed PySCF does not implement perturbative CCSDT(Q)") from exc
            if not isinstance(quadruples, tuple) or len(quadruples) != 2:
                raise CalculationError("Expected PySCF ccsdt_q to return ([Q], (Q)) corrections")
            bracket = _finite_energy(quadruples[0], "[Q] correction", molecule.name)
            correction = _finite_energy(quadruples[1], "(Q) correction", molecule.name)
        if _cancelled(cancel_event):
            raise CalculationCancelled()
        correlation = _finite_energy(correlation + correction, "correlation energy", molecule.name)
        total = _finite_energy(hf_energy + correlation, "total energy", molecule.name)
        return {
            "energy_hartree": total,
            "hf_hartree": hf_energy,
            "correlation_hartree": correlation,
            "perturbative_correction_hartree": correction,
            **({"quadruples_bracket_hartree": bracket} if method == "CCSDT(Q)" else {}),
            "pyscf_version": pyscf.__version__,
            "reference_type": reference_type,
            "converged": True,
        }


class ReferenceCalculator:
    """Calculate independent molecules in isolated, terminable PySCF processes."""

    def __init__(self, settings=None, *, progress=None, progress_callback=None):
        self.settings = settings or CalculationSettings()
        self.progress = progress or progress_callback

    def _emit(self, value):
        if self.progress:
            self.progress(value)

    def calculate(self, tgz_path, ref_path=None, output_path=None, *, cancel_event=None):
        from .runtime import python_executable, worker_environment, stop_process
        if _cancelled(cancel_event):
            raise CalculationCancelled("Calculation cancelled")
        archive = Path(tgz_path).expanduser().resolve()
        if not archive.exists():
            raise FileNotFoundError(archive)
        output = self._resolve_output(archive, ref_path, output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        report = CalculationReport(output, self.settings.method, self.settings.basis, settings=asdict(self.settings))
        stopped = threading.Event()
        with tempfile.TemporaryDirectory(prefix=".energyhub-", dir=output.parent) as temporary:
            root = Path(temporary)
            _safe_extract(archive, root)
            xyz_files = sorted(path for path in root.rglob("*") if path.suffix.lower() == ".xyz")
            if not xyz_files:
                raise CalculationError("The archive contains no .xyz molecules")
            molecules = [_read_xyz(path) for path in xyz_files]
            by_name = {m.name: m for m in molecules}
            if len(by_name) != len(molecules):
                raise CalculationError("Duplicate molecule names in archive")
            template = self._load_template(root, ref_path, archive)
            if not template.strip():
                template = "\n".join(f"1 {m.name} ? 1" for m in molecules)
            lines = parse_reference(template, by_name)
            if not lines:
                raise CalculationError("The reference contains no reaction rows")
            needed = {name for line in lines for _, name in line.pairs}
            missing = needed - by_name.keys()
            if missing:
                raise CalculationError("Reference molecules missing from archive: " + ", ".join(sorted(missing)))
            molecules = [m for m in molecules if m.name in needed]
            self._emit({"status": "running", "total": len(molecules), "completed": 0})
            interpreter = python_executable()
            # Check every reference's spin support before starting expensive work.
            probe = subprocess.run(
                [interpreter, "-c", "import json; from Energyhub.capabilities import get_capabilities; print(json.dumps(get_capabilities()))"],
                env=worker_environment(), capture_output=True, text=True, timeout=30,
            )
            if probe.returncode:
                raise CalculationError("Unable to inspect PySCF: " + probe.stderr[-2000:])
            capabilities = json.loads(probe.stdout)
            capability = next(row for row in capabilities["methods"] if row["name"] == self.settings.method)
            for molecule in molecules:
                channel = "closed_shell" if molecule.multiplicity == 1 else "open_shell"
                if not capability[channel]:
                    reason = capability.get("reason") or f"{self.settings.method} is unavailable for {channel.replace('_', ' ')} structures"
                    raise CalculationError(f"{molecule.name}: {reason}")

            def calculate_one(index, molecule):
                start = time.monotonic()
                if stopped.is_set() or _cancelled(cancel_event):
                    raise CalculationCancelled("Calculation cancelled")
                work = root / f"worker-{index}"
                work.mkdir()
                request_path, result_path = work / "request.json", work / "result.json"
                request_path.write_text(json.dumps({"xyz": str(molecule.path), "settings": asdict(self.settings)}))
                with (work / "pyscf.log").open("w") as log:
                    process = subprocess.Popen(
                        [interpreter, "-m", "Energyhub.worker", "molecule", str(request_path), str(result_path)],
                        env=worker_environment(self.settings.task_threads, work), cwd=work,
                        stdout=log, stderr=subprocess.STDOUT,
                    )
                    try:
                        while process.poll() is None:
                            if stopped.is_set() or _cancelled(cancel_event):
                                raise CalculationCancelled("Calculation cancelled")
                            time.sleep(0.05)
                        if _cancelled(cancel_event):
                            raise CalculationCancelled("Calculation cancelled")
                        if not result_path.exists():
                            raise CalculationError(f"{molecule.name}: worker exited with code {process.returncode}")
                        payload = json.loads(result_path.read_text())
                        if process.returncode or not payload.get("ok"):
                            raise CalculationError(f"{molecule.name}: {payload.get('error', 'calculation failed')}")
                        details = payload["result"]
                        energy = float(details["energy_hartree"])
                        if not math.isfinite(energy):
                            raise CalculationError(f"{molecule.name}: non-finite energy")
                        return MoleculeResult(molecule.name, energy, self.settings.basis, self.settings.method,
                                              time.monotonic() - start, details=details)
                    finally:
                        stop_process(process)

            with concurrent.futures.ThreadPoolExecutor(max_workers=self.settings.pool_size) as executor:
                futures = [executor.submit(calculate_one, i, molecule) for i, molecule in enumerate(molecules)]
                try:
                    for future in concurrent.futures.as_completed(futures):
                        result = future.result()
                        report.results.append(result)
                        self._emit({"status": "running", "total": len(molecules), "completed": len(report.results),
                                    "molecule": result.to_dict()})
                except BaseException:
                    stopped.set()
                    for future in futures:
                        future.cancel()
                    raise
            if _cancelled(cancel_event):
                raise CalculationCancelled("Calculation cancelled")
            order = {m.name: i for i, m in enumerate(molecules)}
            report.results.sort(key=lambda item: order[item.name])
            energies = {item.name: item.energy_hartree for item in report.results}
            rendered = _render_reference(lines, energies)
            for index, line in enumerate((row for row in lines if row.pairs), 1):
                reaction_h = math.fsum(coefficient * energies[name] for coefficient, name in line.pairs)
                report.reactions.append({
                    "index": index, "expression": " ".join(line.tokens[:2 * len(line.pairs)]),
                    "energy_hartree": reaction_h, "energy_kcal_mol": reaction_h * HARTREE_TO_KCAL,
                    "reference_value": reaction_h * HARTREE_TO_KCAL if line.ratio is not None else reaction_h,
                    "unit": "kcal/mol" if line.ratio is not None else "Hartree", "ratio": line.ratio,
                    "normalized_kcal_mol": reaction_h * HARTREE_TO_KCAL / (line.ratio or 1),
                })
            # Only a fully converged dataset is published as a .ref result.
            staged = root / "completed.ref"
            staged.write_text(rendered, encoding="utf-8")
            staged.replace(output)
        report.finished_at = time.time()
        self._emit({"status": "complete", **report.to_dict()})
        return report

    @staticmethod
    def _resolve_output(archive, ref_path, output_path):
        source = Path(ref_path) if ref_path is not None else archive
        if output_path is None:
            return source.with_name(source.stem + ".completed.ref").resolve()
        candidate = Path(output_path).expanduser().resolve()
        if candidate.is_dir():
            candidate = candidate / (source.stem + ".ref")
        if candidate.suffix.lower() != ".ref":
            raise ValueError("output_path must end with .ref")
        if ref_path and candidate == Path(ref_path).resolve():
            raise ValueError("output_path must differ from the reference template")
        return candidate

    @staticmethod
    def _load_template(root, ref_path, archive):
        if ref_path is not None:
            return Path(ref_path).read_text(encoding="utf-8-sig", errors="strict")
        names = sorted(root.rglob("*.ref"))
        if len(names) > 1:
            raise CalculationError("Specify ref_path when the archive contains multiple .ref files")
        return names[0].read_text(encoding="utf-8-sig") if names else ""


def compute_reference(
    tgz_path: str | os.PathLike[str],
    ref_path: str | os.PathLike[str] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    *,
    method: str = "CCSD(T)",
    basis: str = "3zeta",
    pool_size: int = 1,
    memory_mb: int = 4096,
    task_threads: int = 1,
    cancel_event: Any = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    # Compatibility names used by older API workers.
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    threads: int | None = None,
    thread_pool_size: int | None = None,
    memory_pool_size: int | None = None,
    memory_pool_mb: int | None = None,
    basis_family: str = "cc",
    cbs_pair: str = "34",
) -> CalculationReport:
    """Calculate and write a complete reference file.

    The keyword names intentionally mirror the front-end API.  The returned
    report is JSON-friendly through :meth:`CalculationReport.to_dict`.
    """

    if thread_pool_size is not None:
        if pool_size != 1 and int(pool_size) != int(thread_pool_size):
            raise ValueError("pool_size and thread_pool_size specify different values")
        pool_size = thread_pool_size
    if threads is not None:
        if task_threads != 1 and int(task_threads) != int(threads):
            raise ValueError("task_threads and threads specify different values")
        task_threads = threads
    if memory_pool_size is not None:
        if memory_pool_mb is not None and memory_pool_mb != memory_pool_size:
            raise ValueError("memory_pool_mb and memory_pool_size specify different values")
        memory_pool_mb = memory_pool_size
    if progress is None:
        progress = progress_callback
    settings = CalculationSettings(method, basis, pool_size, memory_mb, task_threads,
                                   memory_pool_mb=memory_pool_mb, basis_family=basis_family, cbs_pair=cbs_pair)
    return ReferenceCalculator(settings, progress=progress).calculate(
        tgz_path, ref_path, output_path, cancel_event=cancel_event
    )


# Compatibility aliases used by early Energyhub API clients.
calculate_reference = compute_reference
calculate_archive = compute_reference


__all__ = [
    "BASIS_ALIASES", "METHODS", "CalculationCancelled", "CalculationError",
    "CalculationSettings", "CalculationReport", "MoleculeResult", "ReferenceCalculator",
    "compute_reference", "calculate_reference", "calculate_archive", "parse_reference",
]
