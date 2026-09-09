"""Génère un jeu de données synthétique (FICTIF) pour prendre en main
gr4j_modelling.R avant que les vraies données (GloFAS + réanalyse
pluie/ETP) ne soient prêtes.

Climat synthétique à une saison des pluies (type soudano-sahélien) ; débit
obtenu par un modèle-jouet à deux réservoirs (PAS le code GR4J réel) —
juste de quoi obtenir un hydrogramme plausible (crues en saison des pluies,
récession en saison sèche) pour tester la mécanique du script R. Ce n'est
en aucun cas une simulation hydrologique réelle d'un bassin existant.
"""
import numpy as np
import pandas as pd

rng = np.random.default_rng(42)

dates = pd.date_range("2000-01-01", "2009-12-31", freq="D")
n = len(dates)
day_of_year = dates.dayofyear.to_numpy()

# --- ETP : cycle saisonnier lisse, plus fort en saison sèche (mars-mai) ---
etp = 4.5 + 2.2 * np.cos(2 * np.pi * (day_of_year - 75) / 365.25)
etp = np.clip(etp + rng.normal(0, 0.3, n), 0.5, None)

# --- Pluie : une saison des pluies (juin-septembre), occurrence + intensité ---
season = np.clip(np.cos(2 * np.pi * (day_of_year - 210) / 365.25), -1, 1)
proba_pluie = np.clip(0.05 + 0.45 * np.maximum(season, 0), 0.02, 0.55)
occurrence = rng.random(n) < proba_pluie
intensite = rng.gamma(shape=1.4, scale=6 + 9 * np.maximum(season, 0), size=n)
pcp = np.where(occurrence, intensite, 0.0)
pcp = np.round(pcp, 1)
etp = np.round(etp, 2)

# --- Débit : réservoir "sol" (production) + réservoir "base" (routage) ---
superficie_km2 = 5000.0
coef_mm_vers_m3s = (superficie_km2 * 10**6) / (86400 * 1000)  # mm/j -> m3/s

capacite_sol = 220.0   # mm
sol = capacite_sol * 0.4
base = 6.0             # mm, stock initial du réservoir de routage
k_vidange_base = 0.05  # fraction du réservoir de base vidangée chaque jour
k_infiltration = 0.18  # fraction de l'excès de pluie qui alimente le réservoir de base
part_directe = 0.06    # fraction de l'excès qui ruisselle directement le jour même

debit_mm = np.empty(n)
for i in range(n):
    et_reel = etp[i] * min(1.0, sol / capacite_sol)
    sol += pcp[i] - et_reel
    exces = max(0.0, sol - capacite_sol)
    sol = min(max(sol, 0.0), capacite_sol)

    base += k_infiltration * exces
    vidange = k_vidange_base * base
    base -= vidange

    debit_mm[i] = vidange + part_directe * exces

debit_m3s = np.round(debit_mm * coef_mm_vers_m3s, 3)

df = pd.DataFrame({
    "date": dates.strftime("%Y-%m-%d"),
    "pcp": pcp,
    "evap": etp,
    "debit": debit_m3s,
})

assert df.isna().sum().sum() == 0
assert (df["debit"] >= 0).all()
assert (df["pcp"] >= 0).all()
assert len(df) == n

out_path = "/home/claude/work/gr4j/donnees_exemple_gr4j.csv"
df.to_csv(out_path, index=False)
print("Ecrit :", out_path, "-", len(df), "lignes,", dates.min().date(), "->", dates.max().date())
print("Pluie totale moyenne annuelle (mm/an) :", round(pcp.sum() / (n / 365.25), 1))
print(df.describe())
