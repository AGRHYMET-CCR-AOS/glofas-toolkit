"""Extraction ponctuelle des données GloFAS déjà téléchargées.

Ce module complète ``glofas_download.py`` : une fois les fichiers mensuels
téléchargés (zip ou grib), il permet d'en extraire les séries temporelles de
débit à des coordonnées précises, fournies par l'utilisateur dans un fichier
CSV avec (au minimum) les colonnes ``ID``, ``LONG`` et ``LAT``.

Le réseau hydrographique du modèle GloFAS ne coïncide pas toujours
exactement avec les coordonnées fournies par l'utilisateur (imprécision de
saisie, décalage GPS, résolution de la grille, etc.). On prévoit donc un
paramètre ``radius_km`` qui élargit la recherche autour de chaque point : la
maille retenue est celle qui présente le débit le plus élevé (donc la plus
susceptible d'appartenir à un chenal) parmi toutes les mailles comprises
dans le rayon indiqué. Avec ``radius_km=0`` (par défaut), on se contente de
la maille géographiquement la plus proche.

Prérequis :
    python -m pip install xarray cfgrib eccodes netCDF4 pandas

Utilisation en notebook ::

    from glofas_extract import extract_glofas_at_points

    series = extract_glofas_at_points(
        points="points.csv",
        input_dir="glofas_data",
        start="1980-01",
        end="1983-12",
        radius_km=6.0,
        output="glofas_extraction",
    )

Utilisation en ligne de commande ::

    python glofas_extract.py --points points.csv --input-dir glofas_data \
        --start 1980-01 --end 1983-12 --radius-km 6 --output glofas_extraction
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

LOG = logging.getLogger("glofas_extract")

EARTH_RADIUS_KM = 6371.0088

# Alias de colonnes acceptés dans le CSV de points (comparaison insensible
# à la casse et aux espaces/accents superflus).
_ID_ALIASES = {"id", "code", "station", "nom", "name", "point", "pointid", "point_id"}
_LON_ALIASES = {"long", "lon", "longitude", "x", "lng"}
_LAT_ALIASES = {"lat", "latitude", "y"}

# Motifs des fichiers produits par glofas_download.py :
# - un seul mois (ancien découpage, ou years_per_request=1 + months=[un seul mois]) :
#   glofas_discharge_{year}_{month:02d}.{extension}
# - un groupe de plusieurs mois/années en une seule requête (years_per_request,
#   voir glofas_download._target_filename) :
#   glofas_discharge_{year_debut}_{month_debut:02d}_a_{year_fin}_{month_fin:02d}.{extension}
_FILENAME_RE = re.compile(r"glofas_discharge_(\d{4})_(\d{2})\.(\w+)$")
_FILENAME_RANGE_RE = re.compile(r"glofas_discharge_(\d{4})_(\d{2})_a_(\d{4})_(\d{2})\.(\w+)$")

_GRIB_EXTENSIONS = {"grib", "grib2", "grb"}
_NETCDF_EXTENSIONS = {"nc", "netcdf", "nc4"}


# --------------------------------------------------------------------------- #
# Résultats
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PointMatch:
    """Décrit la maille de grille retenue pour un point utilisateur."""

    point_id: str
    lon_input: float
    lat_input: float
    lon_pixel: float
    lat_pixel: float
    distance_km: float
    radius_km: float
    method: str
    fallback_nearest: bool  # True si aucune maille valide dans le rayon -> repli sur la plus proche
    moved: bool  # True si la maille retenue diffère de la maille géographiquement la plus proche


# --------------------------------------------------------------------------- #
# Lecture du fichier de points
# --------------------------------------------------------------------------- #


def _normalise_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def _find_column(columns: Sequence[str], aliases: set[str], explicit: str | None, role: str) -> str:
    if explicit is not None:
        if explicit not in columns:
            raise ValueError(f"La colonne '{explicit}' (rôle {role}) est absente du CSV : {list(columns)}")
        return explicit
    normalised = {_normalise_key(c): c for c in columns}
    # 1) correspondance exacte (après normalisation)
    for alias in aliases:
        if alias in normalised:
            return normalised[alias]
    # 2) correspondance partielle (ex. "Longitude (°)" -> contient "longitude" ;
    #    "Code station" -> contient "station"), en essayant les alias les plus
    #    longs d'abord pour limiter les faux positifs.
    for alias in sorted(aliases, key=len, reverse=True):
        for key, original in normalised.items():
            if alias in key or key in alias:
                return original
    raise ValueError(
        f"Impossible de trouver la colonne pour '{role}' parmi {list(columns)}. "
        f"Renommez une colonne en {sorted(aliases)} ou précisez le paramètre correspondant."
    )


def read_points_csv(
    path: str | Path,
    *,
    id_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
    sep: str | None = None,
) -> pd.DataFrame:
    """Charge le CSV utilisateur et retourne un DataFrame normalisé.

    Colonnes en sortie : ``id`` (str), ``lon`` (float), ``lat`` (float).
    Les noms de colonnes du fichier source sont détectés automatiquement
    (``ID``/``LONG``/``LAT`` ou variantes courantes) sauf si l'appelant les
    précise explicitement.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Fichier de points introuvable : {path}")

    df = pd.read_csv(path, sep=sep, engine="python") if sep is None else pd.read_csv(path, sep=sep)
    if df.empty:
        raise ValueError(f"Le fichier de points est vide : {path}")

    columns = list(df.columns)
    id_c = _find_column(columns, _ID_ALIASES, id_col, "ID")
    lon_c = _find_column(columns, _LON_ALIASES, lon_col, "LONG")
    lat_c = _find_column(columns, _LAT_ALIASES, lat_col, "LAT")

    out = pd.DataFrame(
        {
            "id": df[id_c].astype(str).str.strip(),
            "lon": pd.to_numeric(df[lon_c], errors="coerce"),
            "lat": pd.to_numeric(df[lat_c], errors="coerce"),
        }
    )

    bad = out[out["lon"].isna() | out["lat"].isna()]
    if not bad.empty:
        raise ValueError(
            "Coordonnées non numériques ou manquantes pour les identifiants : "
            f"{', '.join(bad['id'].tolist())}"
        )
    out_of_range = out[(out["lon"] < -180) | (out["lon"] > 180) | (out["lat"] < -90) | (out["lat"] > 90)]
    if not out_of_range.empty:
        raise ValueError(
            "Coordonnées hors limites (-180/180, -90/90) pour : "
            f"{', '.join(out_of_range['id'].tolist())}"
        )
    duplicated = out["id"][out["id"].duplicated()]
    if not duplicated.empty:
        LOG.warning("Identifiants dupliqués dans le fichier de points : %s", sorted(set(duplicated)))

    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Sélection des fichiers pour la période demandée
# --------------------------------------------------------------------------- #


def _parse_period_bound(value: str | int | date | None, *, end: bool) -> date | None:
    """Accepte None, 'YYYY', 'YYYY-MM', 'YYYY-MM-DD', un int (année) ou un date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, int):
        return date(value, 12, 31) if end else date(value, 1, 1)
    text = str(value).strip()
    parts = text.split("-")
    try:
        if len(parts) == 1:
            year = int(parts[0])
            return date(year, 12, 31) if end else date(year, 1, 1)
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            if end:
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                return date(year, month, last_day)
            return date(year, month, 1)
        year, month, day = (int(p) for p in parts[:3])
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"Format de date non reconnu : {value!r} (attendu YYYY, YYYY-MM ou YYYY-MM-DD)") from exc


def _file_coverage(name: str) -> tuple[date, date] | None:
    """Période (mois de début, mois de fin) couverte par un fichier
    ``glofas_discharge_...``, d'après son nom -- ``None`` si le nom ne
    correspond à aucun des deux motifs connus (fichier non pertinent, ignoré
    par ``find_period_files``).

    Un fichier groupé (``..._a_...``, voir ``glofas_download.py``,
    ``years_per_request``) peut couvrir uniquement certains mois de chaque
    année dans sa plage (ex. ``months=[6,7,8]``) : la période retournée ici
    est l'enveloppe (du premier au dernier mois demandé), pas forcément
    continue mois par mois. Ce n'est pas un problème de justesse : ça ne
    peut qu'élargir, jamais réduire, l'ensemble des fichiers sélectionnés
    pour une période donnée -- le filtrage exact au pas de temps près se
    fait ensuite sur les horodatages réels du fichier (voir
    ``extract_glofas_at_points``), pas sur cette enveloppe.
    """
    m_range = _FILENAME_RANGE_RE.search(name)
    if m_range:
        y1, m1, y2, m2, _ext = m_range.groups()
        return date(int(y1), int(m1), 1), date(int(y2), int(m2), 1)
    m = _FILENAME_RE.search(name)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        month_date = date(year, month, 1)
        return month_date, month_date
    return None


def find_period_files(
    input_dir: str | Path,
    start: str | int | date | None = None,
    end: str | int | date | None = None,
) -> list[Path]:
    """Liste les fichiers GloFAS (produits par glofas_download.py, un mois ou
    un groupe de plusieurs mois/années par fichier) dont la période couverte
    chevauche ``[start, end]``.
    """
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Dossier de données introuvable : {input_dir}")

    start_d = _parse_period_bound(start, end=False)
    end_d = _parse_period_bound(end, end=True)
    if start_d and end_d and start_d > end_d:
        raise ValueError(f"La date de début ({start_d}) est postérieure à la date de fin ({end_d})")
    query_start = date(start_d.year, start_d.month, 1) if start_d else None
    query_end = date(end_d.year, end_d.month, 1) if end_d else None

    matches: list[tuple[date, Path]] = []
    for candidate in sorted(input_dir.iterdir()):
        coverage = _file_coverage(candidate.name)
        if coverage is None:
            continue
        file_start, file_end = coverage
        if query_start and file_end < query_start:
            continue
        if query_end and file_start > query_end:
            continue
        matches.append((file_start, candidate))

    if not matches:
        raise FileNotFoundError(
            f"Aucun fichier 'glofas_discharge_YYYY_MM.*' (ou groupé, "
            f"'glofas_discharge_YYYY_MM_a_YYYY_MM.*') trouvé dans {input_dir} "
            f"pour la période demandée ({start!r} -> {end!r})."
        )
    matches.sort(key=lambda item: item[0])
    return [path for _, path in matches]


# --------------------------------------------------------------------------- #
# Ouverture des fichiers (zip -> grib/netcdf) et standardisation
# --------------------------------------------------------------------------- #


def _extract_zip_members(zip_path: Path, cache_dir: Path) -> list[Path]:
    """Extrait (une seule fois, avec mise en cache) les fichiers de données
    contenus dans une archive zip GloFAS, et renvoie leurs chemins.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target_subdir = cache_dir / zip_path.stem
    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.namelist() if not m.endswith("/")]
        if not members:
            raise ValueError(f"Archive vide : {zip_path}")
        extracted: list[Path] = []
        need_extract = not target_subdir.is_dir()
        if not need_extract:
            for member in members:
                if not (target_subdir / Path(member).name).is_file():
                    need_extract = True
                    break
        if need_extract:
            target_subdir.mkdir(parents=True, exist_ok=True)
            for member in members:
                dest = target_subdir / Path(member).name
                with archive.open(member) as source, open(dest, "wb") as sink:
                    sink.write(source.read())
        for member in members:
            extracted.append(target_subdir / Path(member).name)
    return extracted


def _engine_for(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    if ext in _GRIB_EXTENSIONS:
        return "cfgrib"
    if ext in _NETCDF_EXTENSIONS:
        return "netcdf4"
    # Par défaut on tente cfgrib (cas le plus courant pour les exports EWDS)
    return "cfgrib"


def _open_raw_dataset(path: Path):
    import xarray as xr

    engine = _engine_for(path)
    try:
        if engine == "cfgrib":
            return [
                xr.open_dataset(
                    path,
                    engine="cfgrib",
                    backend_kwargs={"indexpath": ""},
                )
            ]
        return [xr.open_dataset(path, engine=engine)]
    except Exception as exc:  # plusieurs "hypercubes" cfgrib (clés hétérogènes)
        if engine != "cfgrib":
            raise
        try:
            import cfgrib

            datasets = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
            if not datasets:
                raise
            LOG.warning(
                "%s : plusieurs jeux de données GRIB détectés (%d), fusion en un seul.",
                path.name,
                len(datasets),
            )
            return list(datasets)
        except Exception:
            raise exc


def _standardise_dataset(ds):
    """Renomme les coordonnées vers latitude/longitude/time et aplatit une
    éventuelle dimension 'step' supplémentaire dans la dimension temporelle.
    """
    import xarray as xr

    rename = {}
    if "lat" in ds.coords and "latitude" not in ds.coords:
        rename["lat"] = "latitude"
    if "lon" in ds.coords and "longitude" not in ds.coords:
        rename["lon"] = "longitude"
    if rename:
        ds = ds.rename(rename)
    if "latitude" not in ds.coords or "longitude" not in ds.coords:
        raise ValueError("Coordonnées latitude/longitude introuvables dans le fichier.")

    if "valid_time" in ds.coords:
        time_coord = ds["valid_time"]
    elif "time" in ds.coords:
        time_coord = ds["time"]
    else:
        raise ValueError("Coordonnée temporelle (time/valid_time) introuvable dans le fichier.")

    # On capture les valeurs temporelles AVANT tout renommage de dimension,
    # car un renommage invalide immédiatement les références par nom.
    time_dims = list(time_coord.dims)
    time_values = np.asarray(time_coord.values).reshape(-1)

    if len(time_dims) > 1:
        # Cas 'time' x 'step' (produits de type prévision) : on aplatit en
        # une seule dimension 'datetime' basée sur les horodatages réels.
        ds = ds.stack(datetime=time_dims)
        # Le stack() crée un MultiIndex 'datetime' dont 'time'/'step' sont des
        # niveaux : il faut le supprimer en bloc (avec ses niveaux), pas
        # niveau par niveau, sous peine d'avertissement de dépréciation xarray.
        drop_coords = [c for c in ("time", "step", "valid_time", "datetime") if c in ds.coords]
        if drop_coords:
            ds = ds.drop_vars(drop_coords)
        ds = ds.assign_coords(datetime=("datetime", time_values))
    else:
        dim = time_dims[0]
        drop_coords = [c for c in ("time", "step", "valid_time") if c in ds.coords and c != dim]
        if drop_coords:
            ds = ds.drop_vars(drop_coords)
        if dim != "datetime":
            ds = ds.rename({dim: "datetime"})
        ds = ds.assign_coords(datetime=("datetime", time_values))

    # Grille en ordre croissant pour simplifier les recherches par index.
    if ds["latitude"].values[0] > ds["latitude"].values[-1]:
        ds = ds.sortby("latitude")
    if ds["longitude"].values[0] > ds["longitude"].values[-1]:
        ds = ds.sortby("longitude")
    return ds


def _select_variable(ds, variable: str | None) -> str:
    data_vars = [v for v in ds.data_vars if ds[v].ndim >= 2]
    if variable is not None:
        if variable not in ds.data_vars:
            raise ValueError(f"Variable '{variable}' absente du fichier ; variables disponibles : {data_vars}")
        return variable
    if len(data_vars) == 1:
        return data_vars[0]
    # Heuristique : privilégier une variable dont le nom évoque le débit.
    for candidate in data_vars:
        lowered = candidate.lower()
        if "dis" in lowered or "discharge" in lowered:
            return candidate
    raise ValueError(
        f"Plusieurs variables trouvées ({data_vars}) : précisez le paramètre 'variable'."
    )


def open_month_file(path: Path, variable: str | None, cache_dir: Path):
    """Ouvre un fichier mensuel GloFAS (zip ou grib/netcdf direct) et renvoie
    (DataArray standardisé [datetime, latitude, longitude], nom de variable).
    """
    import xarray as xr

    if path.suffix.lower() == ".zip":
        members = _extract_zip_members(path, cache_dir)
        raw_paths = members
    else:
        raw_paths = [path]

    datasets = []
    for raw_path in raw_paths:
        for ds in _open_raw_dataset(raw_path):
            datasets.append(_standardise_dataset(ds))

    if len(datasets) == 1:
        ds = datasets[0]
    else:
        ds = xr.concat(datasets, dim="datetime", combine_attrs="override")

    ds = ds.sortby("datetime")
    var_name = _select_variable(ds, variable)
    return ds[var_name], var_name


# --------------------------------------------------------------------------- #
# Recalage spatial (nearest / max dans un rayon)
# --------------------------------------------------------------------------- #


def _haversine_km(lat1: float, lon1: float, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r = np.radians(lat2)
    lon2_r = np.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = np.sin(dlat / 2) ** 2 + math.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _nearest_index(values: np.ndarray, target: float) -> int:
    return int(np.abs(values - target).argmin())


def locate_pixel(
    lat_values: np.ndarray,
    lon_values: np.ndarray,
    lat0: float,
    lon0: float,
    radius_km: float,
    method: str,
    stat_grid: np.ndarray | None,
) -> tuple[int, int, float, bool]:
    """Retourne (i_lat, j_lon, distance_km, fallback_nearest) pour un point.

    - radius_km <= 0 : maille géographiquement la plus proche.
    - radius_km > 0 et method == 'nearest' : maille la plus proche parmi
      celles situées dans le rayon.
    - radius_km > 0 et method == 'max' : parmi les mailles situées dans le
      rayon, celle dont la statistique 'stat_grid' (ex. débit moyen ou
      maximal) est la plus élevée -> recalage vers le chenal hydrographique.
    """
    if radius_km <= 0:
        i = _nearest_index(lat_values, lat0)
        j = _nearest_index(lon_values, lon0)
        dist = float(_haversine_km(lat0, lon0, np.array([lat_values[i]]), np.array([lon_values[j]]))[0])
        return i, j, dist, False

    margin = 1.25  # marge de sécurité pour la conversion degrés<->km
    dlat_deg = radius_km / 111.32 * margin
    cos_lat = max(math.cos(math.radians(lat0)), 1e-6)
    dlon_deg = radius_km / (111.32 * cos_lat) * margin

    lat_mask = np.abs(lat_values - lat0) <= dlat_deg
    lon_mask = np.abs(lon_values - lon0) <= dlon_deg
    lat_idx = np.where(lat_mask)[0]
    lon_idx = np.where(lon_mask)[0]

    if lat_idx.size == 0 or lon_idx.size == 0:
        i = _nearest_index(lat_values, lat0)
        j = _nearest_index(lon_values, lon0)
        dist = float(_haversine_km(lat0, lon0, np.array([lat_values[i]]), np.array([lon_values[j]]))[0])
        return i, j, dist, True

    lat_sub = lat_values[lat_idx]
    lon_sub = lon_values[lon_idx]
    lon_grid, lat_grid = np.meshgrid(lon_sub, lat_sub)
    dist_grid = _haversine_km(lat0, lon0, lat_grid.ravel(), lon_grid.ravel())
    dist_grid = dist_grid.reshape(lat_sub.size, lon_sub.size)
    within = dist_grid <= radius_km

    if not within.any():
        i = _nearest_index(lat_values, lat0)
        j = _nearest_index(lon_values, lon0)
        dist = float(_haversine_km(lat0, lon0, np.array([lat_values[i]]), np.array([lon_values[j]]))[0])
        return i, j, dist, True

    if method == "max":
        assert stat_grid is not None
        sub_stat = stat_grid[np.ix_(lat_idx, lon_idx)]
        masked = np.where(within, sub_stat, -np.inf)
        flat = int(np.nanargmax(masked))
    else:  # "nearest" contraint au rayon
        masked = np.where(within, dist_grid, np.inf)
        flat = int(np.argmin(masked))

    ii, jj = np.unravel_index(flat, dist_grid.shape)
    i = int(lat_idx[ii])
    j = int(lon_idx[jj])
    return i, j, float(dist_grid[ii, jj]), False


# --------------------------------------------------------------------------- #
# API principale
# --------------------------------------------------------------------------- #


def extract_glofas_at_points(
    points: str | Path | pd.DataFrame,
    input_dir: str | Path = "glofas_data",
    *,
    start: str | int | date | None = None,
    end: str | int | date | None = None,
    radius_km: float = 0.0,
    method: str = "max",
    agg_stat: str = "mean",
    variable: str | None = None,
    id_col: str | None = None,
    lon_col: str | None = None,
    lat_col: str | None = None,
    cache_dir: str | Path | None = None,
    output: str | Path | None = None,
    make_report: bool = False,
) -> pd.DataFrame:
    """Extrait les séries temporelles GloFAS aux points fournis.

    Paramètres
    ----------
    points : chemin vers un CSV (colonnes ID, LONG, LAT ou équivalent) ou
        DataFrame déjà chargé (voir ``read_points_csv``).
    input_dir : dossier contenant les fichiers téléchargés par
        ``glofas_download.py`` (zip ou grib).
    start, end : bornes de la période à extraire (``'1980'``, ``'1980-06'``,
        ``'1980-06-15'``, un entier d'année, ou ``None`` pour ne pas
        borner). ``start``/``end`` filtrent d'abord les fichiers mensuels,
        puis les pas de temps individuels à l'intérieur de chaque fichier.
    radius_km : rayon de recherche (km) autour de chaque point pour
        compenser l'imprécision des coordonnées fournies. ``0`` (défaut
        implicite si non précisé) = uniquement la maille la plus proche.
    method : ``'max'`` (recale sur la maille de plus fort débit dans le
        rayon -> réseau hydrographique) ou ``'nearest'`` (maille la plus
        proche, éventuellement contrainte au rayon). Sans effet si
        ``radius_km <= 0``.
    agg_stat : statistique temporelle utilisée pour choisir la maille en
        méthode ``'max'`` : ``'mean'`` (défaut, robuste) ou ``'max'`` (pic
        de crue). Le point retenu est ensuite fixe pour toute la période
        extraite (pas de changement de maille d'un pas de temps à l'autre).
    variable : nom de variable GloFAS à extraire ; auto-détecté si un seul
        variable est présente dans les fichiers.
    id_col, lon_col, lat_col : noms de colonnes à utiliser si l'auto-
        détection du CSV de points échoue.
    cache_dir : dossier où extraire les zip (par défaut : ``<input_dir>/_extracted``).
    output : si fourni, écrit ``<output>_series.csv`` (séries temporelles,
        format long) et ``<output>_points.csv`` (résumé du recalage par
        point).
    make_report : si True (et ``output`` fourni), génère en plus une carte
        interactive et un rapport HTML autonome via ``glofas_visualize``
        (``pip install folium plotly`` requis) : ``<output>_carte.html``,
        ``<output>_series.html`` et ``<output>_rapport.html``.

    Retour
    ------
    DataFrame au format long : id, lon_input, lat_input, lat_pixel,
    lon_pixel, distance_km, date, variable, value.
    """
    if method not in {"max", "nearest"}:
        raise ValueError("method doit valoir 'max' ou 'nearest'")
    if agg_stat not in {"mean", "max"}:
        raise ValueError("agg_stat doit valoir 'mean' ou 'max'")
    if radius_km < 0:
        raise ValueError("radius_km ne peut pas être négatif")

    points_df = points if isinstance(points, pd.DataFrame) else read_points_csv(
        points, id_col=id_col, lon_col=lon_col, lat_col=lat_col
    )

    input_dir = Path(input_dir)
    cache_dir = Path(cache_dir) if cache_dir is not None else input_dir / "_extracted"
    files = find_period_files(input_dir, start, end)
    LOG.info("Période demandée -> %d fichier(s) mensuel(s) trouvé(s) dans %s", len(files), input_dir)

    start_d = _parse_period_bound(start, end=False)
    end_d = _parse_period_bound(end, end=True)

    series_frames: list[pd.DataFrame] = []
    matches: dict[str, PointMatch] = {}
    variable_name: str | None = None

    for file_index, path in enumerate(files):
        LOG.info("Lecture %s (%d/%d)", path.name, file_index + 1, len(files))
        data_array, var_name = open_month_file(path, variable, cache_dir)
        variable_name = var_name

        if start_d is not None or end_d is not None:
            times = pd.to_datetime(data_array["datetime"].values)
            keep = np.ones(len(times), dtype=bool)
            if start_d is not None:
                keep &= times >= pd.Timestamp(start_d)
            if end_d is not None:
                keep &= times <= pd.Timestamp(end_d)
            data_array = data_array.isel(datetime=keep)
            if data_array.sizes.get("datetime", 0) == 0:
                continue

        lat_values = data_array["latitude"].values
        lon_values = data_array["longitude"].values

        need_stat = method == "max" and radius_km > 0
        stat_grid = None
        if need_stat:
            reducer = getattr(data_array, agg_stat)
            stat_grid = reducer(dim="datetime", skipna=True).values

        values = data_array.values  # (datetime, latitude, longitude)
        times = pd.to_datetime(data_array["datetime"].values)

        for row in points_df.itertuples():
            point_id = row.id
            if point_id not in matches:
                i, j, dist, fallback = locate_pixel(
                    lat_values, lon_values, row.lat, row.lon, radius_km, method, stat_grid
                )
                i_nearest = _nearest_index(lat_values, row.lat)
                j_nearest = _nearest_index(lon_values, row.lon)
                matches[point_id] = PointMatch(
                    point_id=point_id,
                    lon_input=row.lon,
                    lat_input=row.lat,
                    lon_pixel=float(lon_values[j]),
                    lat_pixel=float(lat_values[i]),
                    distance_km=dist,
                    radius_km=radius_km,
                    method=method if radius_km > 0 else "nearest",
                    fallback_nearest=fallback,
                    moved=(i != i_nearest or j != j_nearest),
                )
            match = matches[point_id]
            i = _nearest_index(lat_values, match.lat_pixel)
            j = _nearest_index(lon_values, match.lon_pixel)
            point_values = values[:, i, j]
            series_frames.append(
                pd.DataFrame(
                    {
                        "id": point_id,
                        "date": times,
                        "value": point_values,
                    }
                )
            )

    if not series_frames:
        raise ValueError("Aucune donnée extraite : vérifiez la période demandée par rapport aux fichiers disponibles.")

    series = pd.concat(series_frames, ignore_index=True)
    series = series.sort_values(["id", "date"]).drop_duplicates(["id", "date"]).reset_index(drop=True)
    series["variable"] = variable_name

    meta = pd.DataFrame(
        [
            {
                "id": m.point_id,
                "lon_input": m.lon_input,
                "lat_input": m.lat_input,
                "lon_pixel": round(m.lon_pixel, 5),
                "lat_pixel": round(m.lat_pixel, 5),
                "distance_km": round(m.distance_km, 3),
                "radius_km": m.radius_km,
                "method": m.method,
                "repli_sur_plus_proche": m.fallback_nearest,
                "recale": m.moved,
                "statut": "repli" if m.fallback_nearest else ("recale" if m.moved else "ok"),
            }
            for m in matches.values()
        ]
    )

    far = meta[meta["distance_km"] > max(radius_km, 0.0) + 1e-6]
    if radius_km > 0 and not far.empty:
        LOG.warning(
            "Repli sur la maille la plus proche (aucune maille valide dans le rayon) pour : %s",
            ", ".join(meta.loc[meta["repli_sur_plus_proche"], "id"].tolist()) or "aucun",
        )

    result = series.merge(meta, on="id", how="left")
    result = result[
        [
            "id",
            "lon_input",
            "lat_input",
            "lon_pixel",
            "lat_pixel",
            "distance_km",
            "date",
            "variable",
            "value",
        ]
    ]

    if output is not None:
        output_path = Path(output)
        series_path = output_path.with_name(output_path.name + "_series.csv") if output_path.suffix == "" else output_path
        points_path = output_path.with_name(
            (output_path.stem if output_path.suffix else output_path.name) + "_points.csv"
        )
        series_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(series_path, index=False)
        meta.to_csv(points_path, index=False)
        LOG.info("Écrit : %s (%d lignes) et %s (%d points)", series_path, len(result), points_path, len(meta))

        if make_report:
            try:
                from glofas_visualize import build_report
            except ImportError as exc:
                LOG.warning(
                    "make_report=True mais glofas_visualize est indisponible (%s). "
                    "Installez folium et plotly : pip install folium plotly", exc,
                )
            else:
                report_prefix = output_path.with_suffix("") if output_path.suffix else output_path
                build_report(result, meta, report_prefix)

    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extrait les séries temporelles GloFAS déjà téléchargées à des points donnés."
    )
    parser.add_argument("--points", required=True, help="CSV des points (colonnes ID, LONG, LAT)")
    parser.add_argument("--input-dir", default="glofas_data", help="Dossier des fichiers téléchargés")
    parser.add_argument("--start", default=None, help="Début de période (YYYY, YYYY-MM ou YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Fin de période (YYYY, YYYY-MM ou YYYY-MM-DD)")
    parser.add_argument(
        "--radius-km",
        type=float,
        default=0.0,
        help="Rayon de recherche en km autour de chaque point (0 = maille la plus proche uniquement)",
    )
    parser.add_argument(
        "--method",
        choices=["max", "nearest"],
        default="max",
        help="Critère de sélection dans le rayon : 'max' (débit le plus fort, recalage réseau) ou 'nearest'",
    )
    parser.add_argument(
        "--agg-stat",
        choices=["mean", "max"],
        default="mean",
        help="Statistique temporelle utilisée par la méthode 'max' pour choisir la maille",
    )
    parser.add_argument("--variable", default=None, help="Nom de variable à extraire (auto-détecté sinon)")
    parser.add_argument("--id-col", default=None)
    parser.add_argument("--lon-col", default=None)
    parser.add_argument("--lat-col", default=None)
    parser.add_argument("--cache-dir", default=None, help="Dossier de cache pour l'extraction des zip")
    parser.add_argument("--output", required=True, help="Préfixe des fichiers de sortie (ex: resultats/extraction)")
    parser.add_argument(
        "--report",
        action="store_true",
        help="Génère en plus une carte interactive et un rapport HTML (pip install folium plotly)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        extract_glofas_at_points(
            points=args.points,
            input_dir=args.input_dir,
            start=args.start,
            end=args.end,
            radius_km=args.radius_km,
            method=args.method,
            agg_stat=args.agg_stat,
            variable=args.variable,
            id_col=args.id_col,
            lon_col=args.lon_col,
            lat_col=args.lat_col,
            cache_dir=args.cache_dir,
            output=args.output,
            make_report=args.report,
        )
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
