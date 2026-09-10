"""Téléchargement robuste des données historiques GloFAS depuis l'EWDS.

Prérequis :
    python -m pip install "cdsapi>=0.7.7"

Le fichier ``~/.cdsapirc`` doit contenir l'URL de l'EWDS et la clé API.
Il faut également avoir accepté la licence du jeu de données dans le portail.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import socket
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

DATASET = "cems-glofas-historical"
EWDS_URL = "https://ewds.climate.copernicus.eu/api"
LOG = logging.getLogger("glofas")


@dataclass(frozen=True)
class DownloadResult:
    year: int
    month: int
    path: Path
    status: str  # "downloaded" ou "skipped"
    year_end: int | None = None  # dernière année couverte (requêtes groupées, voir years_per_request)
    month_end: int | None = None  # dernier mois couvert (idem)


def _normalise_ints(values: Iterable[int | str], minimum: int, maximum: int, name: str) -> list[int]:
    try:
        result = sorted({int(value) for value in values})
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} doit contenir uniquement des entiers") from exc
    if not result or result[0] < minimum or result[-1] > maximum:
        raise ValueError(f"{name} doit être compris entre {minimum} et {maximum}")
    return result


def _validate_area(area: Sequence[float]) -> tuple[float, float, float, float]:
    if len(area) != 4:
        raise ValueError("area doit contenir quatre valeurs : nord ouest sud est")
    north, west, south, east = map(float, area)
    if not (-90 <= south < north <= 90):
        raise ValueError("la zone doit respecter -90 <= sud < nord <= 90")
    if not (-180 <= west < east <= 180):
        raise ValueError("la zone doit respecter -180 <= ouest < est <= 180")
    return north, west, south, east


def _is_complete(path: Path, download_format: str) -> bool:
    """Évite de considérer un fichier vide ou une archive corrompue comme terminé."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if download_format == "zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return archive.testzip() is None and bool(archive.infolist())
        except (OSError, zipfile.BadZipFile):
            return False
    return True


def _configured_url(config_path: Path) -> str:
    """Lit le champ ``url:`` du fichier de config cdsapi (format simple
    ``clé: valeur``, pas besoin de PyYAML) ; retombe sur ``EWDS_URL`` si la
    lecture échoue pour une raison quelconque -- utilisé seulement pour la
    vérification réseau ci-dessous, jamais pour la requête elle-même
    (laissée à cdsapi.Client()).
    """
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("url:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return EWDS_URL


def _check_network(url: str, *, timeout: float = 8.0) -> None:
    """Vérification rapide (DNS + connexion TCP) avant d'utiliser cdsapi.

    En cas de coupure réseau, DNS bloqué par un pare-feu/VPN d'entreprise,
    etc., cdsapi retente en interne jusqu'à 500 fois (toutes les 120 s, soit
    potentiellement plusieurs *heures*) avant d'abandonner, avec un message
    peu clair ("Recovering from connection error... attempt 1 of 500"). On
    échoue ici volontairement vite, avec un message exploitable, plutôt que
    de laisser un notebook tourner en boucle sans qu'on sache pourquoi.

    Ne couvre que le tout début du téléchargement (création du client) : une
    coupure survenant en cours de route (entre deux mois) n'est pas
    re-vérifiée ici -- c'est le mécanisme de nouvelle tentative de
    ``_download_glofas_discharge_year`` qui prend le relais dans ce cas (voir
    le message de chaque tentative, qui inclut désormais la cause exacte).
    """
    host = urlparse(url).hostname or url
    try:
        socket.create_connection((host, 443), timeout=timeout).close()
    except OSError as exc:
        raise RuntimeError(
            f"Impossible de joindre {host} ({exc}). Vérifiez votre connexion Internet "
            "(et un éventuel VPN/pare-feu/proxy d'entreprise qui bloquerait ce domaine) "
            "avant de relancer. Sans cette vérification, cdsapi retenterait en interne "
            "pendant potentiellement plusieurs heures (jusqu'à 500 tentatives espacées "
            "de 120 s) sans message d'erreur clair entre-temps."
        ) from exc


def _make_client() -> Any:
    try:
        import cdsapi
    except ImportError as exc:
        raise RuntimeError(
            'Le paquet cdsapi manque. Installez-le avec : python -m pip install "cdsapi>=0.7.7"'
        ) from exc

    # Respecte CDSAPI_RC si défini (cas du notebook de l'atelier, qui utilise
    # un fichier de config nommé différemment de ~/.cdsapirc), sinon le
    # chemin par défaut de cdsapi.
    config = Path(os.environ.get("CDSAPI_RC", str(Path.home() / ".cdsapirc")))
    if not config.is_file():
        raise RuntimeError(
            f"Configuration EWDS absente : créez {config} (ou définissez la variable "
            "d'environnement CDSAPI_RC) avec les champs url et key."
        )
    _check_network(_configured_url(config))
    return cdsapi.Client()


def _batch_years(years: list[int], years_per_request: int) -> list[list[int]]:
    """Regroupe une liste d'années (triée, sans doublon) en groupes d'années
    **consécutives**, de taille au plus ``years_per_request``.

    Ne regroupe jamais des années non consécutives dans le même groupe (même
    si ``years_per_request`` le permettrait) : le nom de fichier produit pour
    un groupe encode sa plage ``année_début-année_fin`` (voir
    ``_target_filename``), qui ne serait plus fiable pour la reprise/la
    sélection des fichiers en aval (``glofas_extract.find_period_files``) si
    la plage contenait un trou.
    """
    if years_per_request < 1:
        raise ValueError("years_per_request doit être supérieur ou égal à 1")
    batches: list[list[int]] = []
    current: list[int] = []
    for y in years:
        if current and (y != current[-1] + 1 or len(current) >= years_per_request):
            batches.append(current)
            current = []
        current.append(y)
    if current:
        batches.append(current)
    return batches


def _target_filename(years: list[int], months: list[int], extension: str) -> str:
    """Nom de fichier pour un groupe d'années/mois demandés en une requête.

    Un groupe d'une seule année et d'un seul mois reprend exactement l'ancien
    nommage ``glofas_discharge_{année}_{mois}.{ext}`` (rétrocompatible avec
    les fichiers déjà téléchargés avant ce correctif -- ``glofas_extract.py``
    continue de les reconnaître). Un groupe plus large est nommé par sa plage
    ``glofas_discharge_{année_début}_{mois_début}_a_{année_fin}_{mois_fin}.{ext}``.
    """
    y1, y2 = years[0], years[-1]
    m1, m2 = months[0], months[-1]
    if y1 == y2 and m1 == m2:
        return f"glofas_discharge_{y1}_{m1:02d}.{extension}"
    return f"glofas_discharge_{y1}_{m1:02d}_a_{y2}_{m2:02d}.{extension}"


def _download_glofas_discharge_batch(
    years: list[int],
    *,
    months: Iterable[int | str] = range(1, 13),
    area: Sequence[float] = (90, -180, -60, 180),
    system_version: str = "version_5_0",
    hydrological_model: str = "lisflood",
    product_type: str = "consolidated",
    timespan: str = "time_mean",
    variable: str = "average_river_discharge_in_the_last_24_hours",
    data_format: str = "grib",
    download_format: str = "zip",
    output_dir: str | os.PathLike[str] = "glofas_data",
    retries: int = 4,
    retry_delay: float = 30.0,
    overwrite: bool = False,
    client: Any | None = None,
) -> list[DownloadResult]:
    """Télécharge un groupe d'années **consécutives** en une seule requête EWDS.

    Contrairement à l'ancien découpage mensuel (une requête par mois), tous
    les mois sélectionnés pour toutes les années du groupe sont demandés en
    un seul appel ``api.retrieve`` -- réduit fortement le nombre de requêtes,
    au prix d'une reprise moins fine (toute la requête est retentée en cas
    d'échec, pas seulement un mois) et d'une requête plus volumineuse (voir
    ``years_per_request`` sur ``download_glofas_discharge`` pour le
    compromis).
    """
    if not years:
        raise ValueError("years ne peut pas être vide")
    for y in years:
        if y < 1979 or y > date.today().year:
            raise ValueError("year doit être compris entre 1979 et l'année courante")
    selected_months = _normalise_ints(months, 1, 12, "months")
    selected_area = _validate_area(area)
    if retries < 1:
        raise ValueError("retries doit être supérieur ou égal à 1")
    if retry_delay < 0:
        raise ValueError("retry_delay ne peut pas être négatif")

    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    api = client or _make_client()
    extension = "zip" if download_format == "zip" else data_format

    target = destination / _target_filename(years, selected_months, extension)
    partial = target.with_name(target.name + ".part")
    label = f"{years[0]}" if len(years) == 1 else f"{years[0]}-{years[-1]}"

    if not overwrite and _is_complete(target, download_format):
        LOG.info("[%s] déjà présent : %s", label, target)
        return [DownloadResult(years[0], selected_months[0], target, "skipped", years[-1], selected_months[-1])]

    # Jours 1-31 pour tous les mois demandés (convention standard des requêtes
    # CDS/EWDS portant sur plusieurs mois à la fois) : le serveur ignore
    # silencieusement les combinaisons inexistantes (ex. 30 février) plutôt
    # que de les rejeter -- pas besoin de calculer le nombre de jours exact
    # par mois comme avec l'ancien découpage mensuel.
    days = [f"{day:02d}" for day in range(1, 32)]
    request = {
        "system_version": [system_version],
        "hydrological_model": [hydrological_model],
        "product_type": [product_type],
        "timespan": [timespan],
        "variable": [variable],
        "year": [str(y) for y in years],
        "month": [f"{m:02d}" for m in selected_months],
        "day": days,
        "data_format": data_format,
        "download_format": download_format,
        "area": list(selected_area),
    }

    for attempt in range(1, retries + 1):
        try:
            partial.unlink(missing_ok=True)
            LOG.info(
                "[%s] envoi (tentative %d/%d, %d année(s) x %d mois)",
                label, attempt, retries, len(years), len(selected_months),
            )
            api.retrieve(DATASET, request, str(partial))
            if not _is_complete(partial, download_format):
                raise RuntimeError("le fichier reçu est vide ou corrompu")
            os.replace(partial, target)
            LOG.info("[%s] terminé : %s", label, target)
            return [DownloadResult(years[0], selected_months[0], target, "downloaded", years[-1], selected_months[-1])]
        except Exception as exc:
            partial.unlink(missing_ok=True)
            if attempt == retries:
                LOG.exception("[%s] échec définitif", label)
                raise
            wait = retry_delay * (2 ** (attempt - 1)) + random.uniform(0, retry_delay * 0.1)
            LOG.warning(
                "[%s] échec temporaire (%s) ; nouvelle tentative dans %.1f s",
                label, exc, wait,
            )
            time.sleep(wait)

    return []  # inatteignable (la boucle ci-dessus retourne ou lève à chaque itération)


def download_glofas_discharge(
    year: int | str | Iterable[int | str],
    *,
    months: Iterable[int | str] = range(1, 13),
    area: Sequence[float] = (90, -180, -60, 180),
    system_version: str = "version_5_0",
    hydrological_model: str = "lisflood",
    product_type: str = "consolidated",
    timespan: str = "time_mean",
    variable: str = "average_river_discharge_in_the_last_24_hours",
    data_format: str = "grib",
    download_format: str = "zip",
    output_dir: str | os.PathLike[str] = "glofas_data",
    years_per_request: int = 1,
    retries: int = 4,
    retry_delay: float = 30.0,
    pause_between_years: float = 5.0,
    overwrite: bool = False,
    client: Any | None = None,
) -> list[DownloadResult]:
    """Télécharge une ou plusieurs années GLOFAS.

    ``year`` accepte une année seule ou tout itérable d'années, par exemple
    ``1980``, ``[1980, 1981]`` ou ``range(1980, 1990)``.

    ``years_per_request`` (``1`` par défaut) regroupe les années
    **consécutives** en une seule requête EWDS par groupe -- tous les mois
    sélectionnés (``months``) pour toutes les années du groupe sont demandés
    en un seul appel, au lieu d'une requête par mois comme avant. Même avec
    la valeur par défaut (``1``), c'est déjà un gain important : une requête
    par année plutôt que douze. Augmenter cette valeur (ex. ``5``) regroupe
    plusieurs années dans une même requête et réduit encore le nombre total
    de requêtes envoyées -- au prix d'une reprise moins fine en cas d'échec
    (toute la requête est retentée, pas seulement l'année en cause) et d'une
    requête plus volumineuse (risque accru de dépasser une limite de
    taille/temps côté serveur -- à ajuster empiriquement ; commencez petit
    et augmentez si ça passe). Les groupes ne franchissent jamais un trou
    dans les années demandées (ex. ``year=[1980, 1981, 1985]`` avec
    ``years_per_request=5`` donne les groupes ``[1980, 1981]`` et ``[1985]``,
    pas un seul groupe de 1980 à 1985).

    Si une requête échoue définitivement (toutes les tentatives épuisées),
    le groupe correspondant est journalisé en erreur et le téléchargement
    **continue** avec les groupes suivants (ne bloque pas tout le
    téléchargement pour un seul groupe en échec, comme avant avec le
    découpage par année) ; une erreur récapitulative est levée à la fin si
    au moins un groupe a échoué, avec la liste des années concernées --
    les groupes réussis restent téléchargés sur disque, relancer l'appel
    reprend uniquement les groupes manquants (``overwrite=False`` par
    défaut).
    """
    if isinstance(year, (int, str)):
        years = [int(year)]
    else:
        try:
            years = [int(value) for value in year]
        except (TypeError, ValueError) as exc:
            raise ValueError("year doit être une année ou un itérable d'années") from exc
    years = sorted(set(years))

    if not years:
        raise ValueError("year ne peut pas être vide")
    if pause_between_years < 0:
        raise ValueError("pause_between_years ne peut pas être négatif")
    if years_per_request < 1:
        raise ValueError("years_per_request doit être supérieur ou égal à 1")

    # Matérialiser les mois une seule fois : un générateur doit rester utilisable
    # pour chacun des groupes d'années demandés.
    selected_months = list(months)
    batches = _batch_years(years, years_per_request)
    api = client or _make_client()
    all_results: list[DownloadResult] = []
    failed_batches: list[list[int]] = []

    for index, batch in enumerate(batches):
        label = f"{batch[0]}" if len(batch) == 1 else f"{batch[0]}-{batch[-1]}"
        try:
            all_results.extend(
                _download_glofas_discharge_batch(
                    batch,
                    months=selected_months,
                    area=area,
                    system_version=system_version,
                    hydrological_model=hydrological_model,
                    product_type=product_type,
                    timespan=timespan,
                    variable=variable,
                    data_format=data_format,
                    download_format=download_format,
                    output_dir=output_dir,
                    retries=retries,
                    retry_delay=retry_delay,
                    overwrite=overwrite,
                    client=api,
                )
            )
        except Exception as exc:
            LOG.error("[%s] groupe d'années en échec définitif : %s", label, exc)
            failed_batches.append(batch)
        if index < len(batches) - 1 and pause_between_years:
            LOG.info("Pause de %.1f s avant le groupe d'années suivant", pause_between_years)
            time.sleep(pause_between_years)

    if failed_batches:
        failed_years = sorted(y for batch in failed_batches for y in batch)
        raise RuntimeError(
            f"Échec du téléchargement pour {len(failed_years)} année(s) : {failed_years} "
            "(voir les logs ci-dessus pour le détail de chaque groupe). Les autres années "
            "ont été téléchargées normalement ; relancez cet appel pour ne reprendre que "
            "les groupes manquants (overwrite=False par défaut)."
        )

    return all_results


def _parse_years(value: str) -> list[int]:
    """Accepte 1980, 1980-1989 ou 1980,1982,1985-1987."""
    years: set[int] = set()
    try:
        for item in value.split(","):
            bounds = item.strip().split("-", 1)
            if len(bounds) == 1:
                years.add(int(bounds[0]))
            else:
                start, end = map(int, bounds)
                if start > end:
                    raise ValueError
                years.update(range(start, end + 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("format attendu : 1980, 1980-1989 ou une combinaison") from exc
    return sorted(years)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Télécharge l'historique GLOFAS depuis l'EWDS")
    parser.add_argument("--years", type=_parse_years, default=_parse_years("1980-1989"))
    parser.add_argument("--months", nargs="+", type=int, default=list(range(1, 13)))
    parser.add_argument("--area", nargs=4, type=float, metavar=("N", "W", "S", "E"), default=(90, -180, -60, 180))
    parser.add_argument("--output-dir", default="glofas_data")
    parser.add_argument(
        "--years-per-request", type=int, default=1,
        help="nombre d'années consécutives regroupées par requête EWDS (1 = une requête par année, "
             "déjà un gain net par rapport à l'ancien découpage mensuel ; augmenter réduit encore "
             "le nombre de requêtes, au prix d'une reprise moins fine en cas d'échec)",
    )
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=30.0)
    parser.add_argument("--pause", type=float, default=5.0, help="pause entre deux requêtes (groupes d'années)")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if args.pause < 0:
        LOG.error("--pause ne peut pas être négatif")
        return 2
    if args.years_per_request < 1:
        LOG.error("--years-per-request doit être supérieur ou égal à 1")
        return 2

    try:
        client = _make_client()
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 2

    try:
        results = download_glofas_discharge(
            args.years,
            months=args.months,
            area=args.area,
            output_dir=args.output_dir,
            years_per_request=args.years_per_request,
            retries=args.retries,
            retry_delay=args.retry_delay,
            pause_between_years=args.pause,
            overwrite=args.overwrite,
            client=client,
        )
    except RuntimeError as exc:
        # download_glofas_discharge a déjà journalisé chaque groupe en échec ;
        # ce message récapitule et déclenche un code de sortie non nul.
        LOG.error("%s", exc)
        return 1

    downloaded = sum(result.status == "downloaded" for result in results)
    skipped = sum(result.status == "skipped" for result in results)
    LOG.info("Résumé : %d requête(s) téléchargée(s), %d ignorée(s) (déjà présente(s))", downloaded, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
