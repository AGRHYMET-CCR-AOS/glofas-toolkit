"""Téléchargement robuste des données historiques GloFAS depuis l'EWDS.

Prérequis :
    python -m pip install "cdsapi>=0.7.7"

Le fichier ``~/.cdsapirc`` doit contenir l'URL de l'EWDS et la clé API.
Il faut également avoir accepté la licence du jeu de données dans le portail.
"""

from __future__ import annotations

import argparse
import calendar
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


def _download_glofas_discharge_year(
    year: int | str,
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
    """Télécharge une année en fichiers mensuels, avec reprise et contrôle d'intégrité.

    Le découpage mensuel limite la taille des requêtes et permet de reprendre un
    téléchargement interrompu sans recommencer toute l'année.
    """
    year = int(year)
    if year < 1979 or year > date.today().year:
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
    results: list[DownloadResult] = []

    for month in selected_months:
        target = destination / f"glofas_discharge_{year}_{month:02d}.{extension}"
        partial = target.with_name(target.name + ".part")
        if not overwrite and _is_complete(target, download_format):
            LOG.info("[%d-%02d] déjà présent : %s", year, month, target)
            results.append(DownloadResult(year, month, target, "skipped"))
            continue

        days = [f"{day:02d}" for day in range(1, calendar.monthrange(year, month)[1] + 1)]
        request = {
            "system_version": [system_version],
            "hydrological_model": [hydrological_model],
            "product_type": [product_type],
            "timespan": [timespan],
            "variable": [variable],
            "year": [str(year)],
            "month": [f"{month:02d}"],
            "day": days,
            "data_format": data_format,
            "download_format": download_format,
            "area": list(selected_area),
        }

        for attempt in range(1, retries + 1):
            try:
                partial.unlink(missing_ok=True)
                LOG.info("[%d-%02d] envoi (tentative %d/%d)", year, month, attempt, retries)
                api.retrieve(DATASET, request, str(partial))
                if not _is_complete(partial, download_format):
                    raise RuntimeError("le fichier reçu est vide ou corrompu")
                os.replace(partial, target)
                LOG.info("[%d-%02d] terminé : %s", year, month, target)
                results.append(DownloadResult(year, month, target, "downloaded"))
                break
            except Exception as exc:
                partial.unlink(missing_ok=True)
                if attempt == retries:
                    LOG.exception("[%d-%02d] échec définitif", year, month)
                    raise
                wait = retry_delay * (2 ** (attempt - 1)) + random.uniform(0, retry_delay * 0.1)
                LOG.warning(
                    "[%d-%02d] échec temporaire (%s) ; nouvelle tentative dans %.1f s",
                    year, month, exc, wait,
                )
                time.sleep(wait)

    return results


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
    retries: int = 4,
    retry_delay: float = 30.0,
    pause_between_years: float = 5.0,
    overwrite: bool = False,
    client: Any | None = None,
) -> list[DownloadResult]:
    """Télécharge une ou plusieurs années GLOFAS.

    ``year`` accepte une année seule ou tout itérable d'années, par exemple
    ``1980``, ``[1980, 1981]`` ou ``range(1980, 1990)``. Les résultats mensuels
    de toutes les années sont retournés dans une seule liste.
    """
    if isinstance(year, (int, str)):
        years = [int(year)]
    else:
        try:
            years = [int(value) for value in year]
        except (TypeError, ValueError) as exc:
            raise ValueError("year doit être une année ou un itérable d'années") from exc

    if not years:
        raise ValueError("year ne peut pas être vide")
    if pause_between_years < 0:
        raise ValueError("pause_between_years ne peut pas être négatif")

    # Matérialiser les mois une seule fois : un générateur doit rester utilisable
    # pour chacune des années demandées.
    selected_months = list(months)
    api = client or _make_client()
    all_results: list[DownloadResult] = []

    for index, selected_year in enumerate(years):
        all_results.extend(
            _download_glofas_discharge_year(
                selected_year,
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
        if index < len(years) - 1 and pause_between_years:
            LOG.info("Pause de %.1f s avant l'année suivante", pause_between_years)
            time.sleep(pause_between_years)

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
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=30.0)
    parser.add_argument("--pause", type=float, default=5.0, help="pause entre deux années")
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

    try:
        client = _make_client()
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 2

    downloaded = skipped = 0
    failures: list[int] = []
    for index, year in enumerate(args.years):
        try:
            results = download_glofas_discharge(
                year,
                months=args.months,
                area=args.area,
                output_dir=args.output_dir,
                retries=args.retries,
                retry_delay=args.retry_delay,
                overwrite=args.overwrite,
                client=client,
            )
            downloaded += sum(result.status == "downloaded" for result in results)
            skipped += sum(result.status == "skipped" for result in results)
        except Exception as exc:
            LOG.error("[%d] année incomplète : %s", year, exc)
            failures.append(year)
        if index < len(args.years) - 1 and args.pause:
            time.sleep(args.pause)

    LOG.info("Résumé : %d téléchargé(s), %d ignoré(s), %d année(s) en échec", downloaded, skipped, len(failures))
    if failures:
        LOG.error("Années en échec : %s", ", ".join(map(str, failures)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
