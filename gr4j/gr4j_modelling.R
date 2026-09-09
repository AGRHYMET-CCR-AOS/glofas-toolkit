#***************************************************************************#
#         MODELISATION HYDROLOGIQUE AVEC GR4J -- SCRIPT DE FORMATION        #
#***************************************************************************#
#
# Calage et validation du modele pluie-debit journalier GR4J (package
# airGR), simplifie a des fins pedagogiques pour l'atelier de formation des
# experts nationaux des services d'hydrologie d'Afrique centrale (Yaounde,
# sept. 2026).
#
# Par rapport a un usage "recherche" (voir le script dont celui-ci s'inspire),
# ce script simplifie volontairement :
#   - une seule periode de calage et une seule periode de validation
#     independante (au lieu d'une validation croisee a plusieurs blocs) ;
#   - la calibration interne d'airGR (Calibration_Michel), rapide et
#     deterministe, plutot qu'une optimisation externe (DEoptim) couteuse en
#     temps de calcul -- plus adaptee a une demonstration en direct ;
#   - pas de sauvegarde en base SQLite : tout est ecrit en CSV/PNG, plus
#     simple a ouvrir et a comparer entre participants.
#
# DONNEES D'ENTREE ATTENDUES (voir section 1 ci-dessous) :
#   Un fichier CSV avec au moins les colonnes :
#     - date  : date journaliere (AAAA-MM-JJ)
#     - pcp   : precipitation journaliere moyenne sur le bassin (mm/j)
#     - evap  : evapotranspiration potentielle journaliere (mm/j)
#     - debit : debit observe a l'exutoire (m3/s)
#   Le pipeline de preparation de ces donnees (debit de reference GloFAS,
#   pluie/ETP d'une reanalyse) fait l'objet d'un notebook a venir separement ;
#   ce script prend simplement le CSV deja pret en entree, quelle que soit sa
#   source. En attendant, `donnees_exemple_gr4j.csv` (genere par
#   generer_donnees_exemple.py) permet de prendre le script en main des
#   maintenant -- c'est un jeu de donnees FICTIF (climat et debit simules de
#   maniere simplifiee), pas une vraie serie hydrologique.
#
# Prerequis :
#   install.packages(c("airGR", "dplyr", "tidyr", "lubridate", "readr",
#                       "ggplot2", "hydroGOF"))
#
# NOTE : ce script n'a pas pu etre execute contre le package airGR reel dans
# l'environnement ou il a ete redige (pas d'acces a CRAN). Il reprend les
# fonctions et l'enchainement du script original (deja valide dans votre
# environnement), mais merci de le tester une premiere fois de votre cote
# avant l'atelier -- voir le paragraphe "verification" en fin de fichier.


## ------------------------------------------------------------------------ ##
## 0. NETTOYAGE DE L'ENVIRONNEMENT ET CHARGEMENT DES PACKAGES               ##
## ------------------------------------------------------------------------ ##

rm(list = ls())

packages_requis <- c("airGR", "dplyr", "tidyr", "lubridate", "readr", "ggplot2", "hydroGOF")
packages_manquants <- packages_requis[!sapply(packages_requis, requireNamespace, quietly = TRUE)]
if (length(packages_manquants) > 0) {
  install.packages(packages_manquants)
}
invisible(lapply(packages_requis, library, character.only = TRUE))


## ------------------------------------------------------------------------ ##
## 1. PARAMETRES UTILISATEUR -- a adapter a votre bassin et vos donnees     ##
## ------------------------------------------------------------------------ ##

# --- Identification (utilise pour les titres et les noms de fichiers) ---
nom_bassin     <- "Bassin_Test"                  # ex. "Mouhoun" une fois vos vraies donnees branchees
fichier_entree <- "donnees_exemple_gr4j.csv"      # CSV : date, pcp, evap, debit (voir en-tete ci-dessus)
dossier_sortie <- "resultats_gr4j"                # dossier de sortie (cree automatiquement)

# --- Caracteristique du bassin versant ---
superficie_km2 <- 5000                            # superficie (km2), pour convertir le debit observe m3/s -> mm/j

# --- Decoupage temporel ---
# Le modele a besoin d'une periode de "mise en route" (warm-up : les
# reservoirs internes s'initialisent et se stabilisent), puis d'une periode
# de calage (ajustement des 4 parametres de GR4J) et d'une periode de
# validation independante (pour juger la performance sur des donnees non
# vues pendant le calage). Adaptez ces dates a la periode couverte par votre
# fichier -- les valeurs par defaut correspondent au jeu de donnees exemple.
date_debut_mise_en_route <- "2000-01-01"
date_debut_calage        <- "2001-01-01"
date_fin_calage          <- "2007-12-31"
date_debut_validation    <- "2008-01-01"
date_fin_validation      <- "2009-12-31"

# --- Critere de calage ---
# Fonction objectif optimisee lors du calage. "KGE2012" (variante corrigee
# du KGE) est le defaut recommande par airGR ; "NSE" et "KGE" restent tres
# utilises en hydrologie operationnelle.
critere_calage <- "KGE2012"   # un parmi : "NSE", "KGE", "KGE2012"


## ------------------------------------------------------------------------ ##
## 2. CHARGEMENT ET CONTROLE DES DONNEES                                    ##
## ------------------------------------------------------------------------ ##

if (!file.exists(fichier_entree)) {
  stop(
    "Fichier d'entree introuvable : ", fichier_entree,
    "\nVerifiez le chemin, ou generez le jeu de donnees d'exemple avec generer_donnees_exemple.py"
  )
}

donnees <- read_csv(fichier_entree, show_col_types = FALSE)

colonnes_requises <- c("date", "pcp", "evap", "debit")
colonnes_absentes <- setdiff(colonnes_requises, names(donnees))
if (length(colonnes_absentes) > 0) {
  stop(
    "Colonne(s) manquante(s) dans ", fichier_entree, " : ", paste(colonnes_absentes, collapse = ", "),
    "\nColonnes attendues : ", paste(colonnes_requises, collapse = ", ")
  )
}

donnees <- donnees %>%
  mutate(date = as.Date(date)) %>%
  arrange(date) %>%
  distinct(date, .keep_all = TRUE)

# --- Continuite temporelle : airGR exige une serie journaliere sans trou ---
ecarts_dates <- diff(donnees$date)
if (any(ecarts_dates != 1)) {
  trous <- donnees$date[which(ecarts_dates != 1) + 1]
  stop(
    length(trous), " rupture(s) dans la serie temporelle (dates non consecutives), ",
    "par exemple autour de : ", paste(head(trous, 3), collapse = ", "),
    ".\nComblez les lacunes (interpolation, valeurs manquantes explicites) avant de poursuivre."
  )
}

# --- Valeurs manquantes sur les forcages (non tolerees par airGR) ---
n_na_pcp  <- sum(is.na(donnees$pcp))
n_na_evap <- sum(is.na(donnees$evap))
if (n_na_pcp > 0 || n_na_evap > 0) {
  stop(
    "Valeurs manquantes dans les forcages : ", n_na_pcp, " en pcp, ", n_na_evap, " en evap.\n",
    "Comblez-les avant de poursuivre (airGR n'accepte pas de NA dans P/ETP)."
  )
}

# --- Conversion du debit observe de m3/s vers mm/j ---
# Q[mm/j] = Q[m3/s] * 86400[s/j] / Superficie[m2] * 1000[mm/m]
superficie_m2      <- superficie_km2 * 10^6
coef_m3s_vers_mmj  <- (86400 * 1000) / superficie_m2
donnees <- donnees %>%
  mutate(debit_mm = debit * coef_m3s_vers_mmj)

cat(
  "Periode disponible :", format(min(donnees$date)), "->", format(max(donnees$date)),
  "(", nrow(donnees), "jours )\n"
)


## ------------------------------------------------------------------------ ##
## 3. PREPARATION DES ENTREES ET DES PERIODES POUR airGR                    ##
## ------------------------------------------------------------------------ ##

# airGR attend des dates au format POSIXct (pas simplement Date).
dates_posix <- as.POSIXct(donnees$date, tz = "UTC")

InputsModel <- CreateInputsModel(
  FUN_MOD = RunModel_GR4J,
  DatesR  = dates_posix,
  Precip  = donnees$pcp,
  PotEvap = donnees$evap
)

# Indices (positions dans `donnees`) correspondant a chaque periode --------
trouver_indices <- function(date_debut, date_fin, description) {
  indices <- which(donnees$date >= as.Date(date_debut) & donnees$date <= as.Date(date_fin))
  if (length(indices) == 0) {
    stop(
      "Aucune donnee entre ", date_debut, " et ", date_fin, " (periode '", description, "')",
      " -- verifiez que ces dates sont couvertes par ", fichier_entree
    )
  }
  indices
}

Ind_MiseEnRoute <- trouver_indices(date_debut_mise_en_route, as.character(as.Date(date_debut_calage) - 1), "mise en route")
Ind_Calage      <- trouver_indices(date_debut_calage, date_fin_calage, "calage")
Ind_Validation  <- trouver_indices(date_debut_validation, date_fin_validation, "validation")

FUN_CRIT <- switch(critere_calage,
  "NSE"     = ErrorCrit_NSE,
  "KGE"     = ErrorCrit_KGE,
  "KGE2012" = ErrorCrit_KGE2,
  stop("critere_calage doit valoir 'NSE', 'KGE' ou 'KGE2012' (valeur recue : ", critere_calage, ")")
)

RunOptions_Calage <- CreateRunOptions(
  FUN_MOD          = RunModel_GR4J,
  InputsModel      = InputsModel,
  IndPeriod_Run    = Ind_Calage,
  IndPeriod_WarmUp = Ind_MiseEnRoute,
  Outputs_Cal      = "Qsim"
)

InputsCrit <- CreateInputsCrit(
  FUN_CRIT    = FUN_CRIT,
  InputsModel = InputsModel,
  RunOptions  = RunOptions_Calage,
  Obs         = donnees$debit_mm[Ind_Calage]
)

CalibOptions <- CreateCalibOptions(FUN_MOD = RunModel_GR4J, FUN_CALIB = Calibration_Michel)


## ------------------------------------------------------------------------ ##
## 4. CALAGE DES PARAMETRES DE GR4J                                         ##
## ------------------------------------------------------------------------ ##

cat("\nCalage en cours (critere :", critere_calage, ", periode :",
    date_debut_calage, "->", date_fin_calage, ")...\n")

OutputsCalib <- Calibration_Michel(
  InputsModel  = InputsModel,
  RunOptions   = RunOptions_Calage,
  InputsCrit   = InputsCrit,
  CalibOptions = CalibOptions,
  FUN_MOD      = RunModel_GR4J
)

parametres_optimaux <- OutputsCalib$ParamFinalR
names(parametres_optimaux) <- c(
  "X1_capacite_production_mm",
  "X2_echange_souterrain_mm_j",
  "X3_capacite_routage_mm",
  "X4_temps_base_hydrogramme_j"
)

cat("\nParametres GR4J calés :\n")
print(round(parametres_optimaux, 2))
cat(
  "\n  X1 : capacite du reservoir de production -- plus grand = sol qui retient plus d'eau\n",
  "  X2 : echange en nappe (peut etre negatif = pertes, positif = apports exterieurs)\n",
  "  X3 : capacite du reservoir de routage -- regule le tarissement\n",
  "  X4 : temps de base de l'hydrogramme unitaire (j) -- inertie du bassin\n",
  sep = ""
)


## ------------------------------------------------------------------------ ##
## 5. SIMULATION ET EVALUATION SUR LES PERIODES DE CALAGE ET DE VALIDATION  ##
## ------------------------------------------------------------------------ ##

simuler_et_evaluer <- function(indices_periode, nom_periode) {
  RunOptions <- CreateRunOptions(
    FUN_MOD          = RunModel_GR4J,
    InputsModel      = InputsModel,
    IndPeriod_Run    = indices_periode,
    IndPeriod_WarmUp = Ind_MiseEnRoute
  )
  OutputsModel <- RunModel_GR4J(
    InputsModel = InputsModel,
    RunOptions  = RunOptions,
    Param       = parametres_optimaux
  )

  obs <- donnees$debit_mm[indices_periode]
  sim <- OutputsModel$Qsim

  metriques <- suppressWarnings(hydroGOF::gof(sim, obs, method = "2012"))
  cat("\n--- Performance --", nom_periode, "---\n")
  print(round(metriques[c("NSE", "KGE", "PBIAS %", "RMSE"), , drop = FALSE], 3))

  list(
    outputs     = OutputsModel,
    dates       = donnees$date[indices_periode],
    obs         = obs,
    sim         = sim,
    metriques   = metriques,
    nom_periode = nom_periode
  )
}

resultats_calage     <- simuler_et_evaluer(Ind_Calage, "Calage")
resultats_validation <- simuler_et_evaluer(Ind_Validation, "Validation")


## ------------------------------------------------------------------------ ##
## 6. GRAPHIQUES DE DIAGNOSTIC                                              ##
## ------------------------------------------------------------------------ ##

dir.create(dossier_sortie, showWarnings = FALSE, recursive = TRUE)

# 6.1 Diagnostic airGR standard (pluie, debits obs/sim, erreurs cumulees) --
png(file.path(dossier_sortie, paste0(nom_bassin, "_diagnostic_calage.png")), width = 1400, height = 1000, res = 130)
plot(resultats_calage$outputs, Qobs = resultats_calage$obs, main = paste(nom_bassin, "- Calage"))
dev.off()

png(file.path(dossier_sortie, paste0(nom_bassin, "_diagnostic_validation.png")), width = 1400, height = 1000, res = 130)
plot(resultats_validation$outputs, Qobs = resultats_validation$obs, main = paste(nom_bassin, "- Validation"))
dev.off()

# 6.2 Hydrogramme observe vs simule, calage + validation sur un meme graphique
couleur_obs <- "#2a78d6"   # bleu (coherent avec les autres livrables de l'atelier)
couleur_sim <- "#eb6834"   # orange

hydrogramme <- bind_rows(
  tibble(date = resultats_calage$dates,     periode = "Calage",     Observe = resultats_calage$obs,     Simule = resultats_calage$sim),
  tibble(date = resultats_validation$dates, periode = "Validation", Observe = resultats_validation$obs, Simule = resultats_validation$sim)
) %>%
  pivot_longer(cols = c(Observe, Simule), names_to = "serie", values_to = "debit_mm")

p_hydrogramme <- ggplot(hydrogramme, aes(x = date, y = debit_mm, color = serie)) +
  geom_line(linewidth = 0.4) +
  geom_vline(xintercept = as.Date(date_debut_validation), linetype = "dashed", color = "grey40") +
  scale_color_manual(values = c("Observe" = couleur_obs, "Simule" = couleur_sim)) +
  labs(
    title = paste("GR4J -", nom_bassin),
    subtitle = "Trait pointille = debut de la periode de validation",
    x = NULL, y = "Debit (mm/j)", color = NULL
  ) +
  theme_minimal(base_size = 12)

ggsave(file.path(dossier_sortie, paste0(nom_bassin, "_hydrogramme.png")), p_hydrogramme, width = 11, height = 5, dpi = 150)

# 6.3 Nuage de points debit observe vs simule (calage et validation) ------
p_scatter <- bind_rows(
  tibble(periode = "Calage",     Observe = resultats_calage$obs,     Simule = resultats_calage$sim),
  tibble(periode = "Validation", Observe = resultats_validation$obs, Simule = resultats_validation$sim)
) %>%
  ggplot(aes(x = Observe, y = Simule)) +
  geom_point(alpha = 0.3, size = 0.8, color = couleur_obs) +
  geom_abline(slope = 1, intercept = 0, linetype = "dashed", color = "grey40") +
  facet_wrap(~periode) +
  coord_equal() +
  labs(
    title = paste("GR4J -", nom_bassin, "- debit observe vs simule"),
    x = "Debit observe (mm/j)", y = "Debit simule (mm/j)"
  ) +
  theme_minimal(base_size = 12)

ggsave(file.path(dossier_sortie, paste0(nom_bassin, "_obs_vs_sim.png")), p_scatter, width = 9, height = 5, dpi = 150)

cat("\nGraphiques ecrits dans :", normalizePath(dossier_sortie), "\n")


## ------------------------------------------------------------------------ ##
## 7. EXPORT DES RESULTATS (parametres, performance, series simulees)       ##
## ------------------------------------------------------------------------ ##

write_csv(
  tibble(parametre = names(parametres_optimaux), valeur = as.numeric(parametres_optimaux)),
  file.path(dossier_sortie, paste0(nom_bassin, "_parametres_gr4j.csv"))
)

table_performance <- bind_rows(
  tibble(periode = "Calage",     as_tibble(t(resultats_calage$metriques[c("NSE", "KGE", "PBIAS %", "RMSE"), , drop = FALSE]))),
  tibble(periode = "Validation", as_tibble(t(resultats_validation$metriques[c("NSE", "KGE", "PBIAS %", "RMSE"), , drop = FALSE])))
)
write_csv(table_performance, file.path(dossier_sortie, paste0(nom_bassin, "_performance.csv")))

write_csv(
  bind_rows(
    tibble(date = resultats_calage$dates,     periode = "Calage",     debit_observe_mm = resultats_calage$obs,     debit_simule_mm = resultats_calage$sim),
    tibble(date = resultats_validation$dates, periode = "Validation", debit_observe_mm = resultats_validation$obs, debit_simule_mm = resultats_validation$sim)
  ),
  file.path(dossier_sortie, paste0(nom_bassin, "_series_simulees.csv"))
)

cat("\nTermine. Resultats (CSV + PNG) ecrits dans :", normalizePath(dossier_sortie), "\n")

## ------------------------------------------------------------------------ ##
## Verification avant l'atelier                                            ##
## ------------------------------------------------------------------------ ##
# 1. Generez le jeu de donnees exemple :  python generer_donnees_exemple.py
# 2. Lancez ce script tel quel (parametres par defaut = jeu de donnees
#    exemple) et verifiez qu'il se termine sans erreur et produit les
#    fichiers attendus dans resultats_gr4j/.
# 3. Remplacez ensuite fichier_entree, nom_bassin, superficie_km2 et les
#    dates de la section 1 par vos propres donnees.
