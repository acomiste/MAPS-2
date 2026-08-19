"""
Extraction d'informations d'établissements à partir d'URLs Google Maps.
Version parallèle (plusieurs onglets simultanés) + compatible GitHub Actions.

Pré-requis :
    pip install playwright
    playwright install chromium

Utilisation en local :
    python scrape_google_maps.py

Utilisation sur GitHub Actions :
    Voir le fichier .github/workflows/scrape-maps.yml associé.
    Les paramètres ci-dessous peuvent être surchargés par des variables
    d'environnement du même nom (utile pour les régler depuis l'onglet
    "Actions" sans toucher au code).
"""

import asyncio
import csv
import os
import re
import random
import subprocess

from playwright.async_api import async_playwright

# ==========================================================================
# PARAMÈTRES MODIFIABLES
# (chacun peut être surchargé par une variable d'environnement du même nom)
# ==========================================================================

# Nombre d'onglets (pages) traités en parallèle
NB_ONGLETS = int(os.environ.get("NB_ONGLETS", 2))

# Pause aléatoire (en secondes) appliquée APRÈS le traitement de chaque URL,
# avant de passer à la suivante sur un même onglet
PAUSE_MIN = float(os.environ.get("PAUSE_MIN", 2.0))
PAUSE_MAX = float(os.environ.get("PAUSE_MAX", 4.0))

# Nombre d'URLs traitées entre deux commits Git (utile uniquement sur
# GitHub Actions : permet de ne pas perdre la progression en cas de coupure)
GIT_COMMIT_TOUTES_LES = int(os.environ.get("GIT_COMMIT_TOUTES_LES", 20))

# Navigateur visible (False) ou invisible (True). Sur GitHub Actions il n'y a
# pas d'écran : on force donc headless=True automatiquement si la variable
# GITHUB_ACTIONS est présente (définie par défaut par GitHub Actions).
HEADLESS = os.environ.get("HEADLESS", "true" if os.environ.get("GITHUB_ACTIONS") else "false").lower() == "true"

FICHIER_URLS = "urls.csv"
FICHIER_RESULTATS = "resultats.csv"

COLONNES_RESULTATS = [
    "url", "nom", "secteur_activite", "adresse",
    "code_postal", "ville", "pays", "telephone", "site_web"
]


# --------------------------------------------------------------------------
# Gestion du fichier d'URLs (lecture + mise à jour du statut)
# --------------------------------------------------------------------------

def lire_urls():
    if not os.path.exists(FICHIER_URLS):
        print(f"❌ Fichier '{FICHIER_URLS}' introuvable.")
        return []

    with open(FICHIER_URLS, encoding="utf-8-sig") as f:
        lignes = [l.strip() for l in f.read().splitlines() if l.strip()]

    if not lignes:
        return []

    if lignes[0].lower().startswith("http"):
        return [{"url": l, "statut": ""} for l in lignes]

    reader = csv.DictReader(lignes)
    lignes_dict = []
    for r in reader:
        lignes_dict.append({
            "url": (r.get("url") or "").strip(),
            "statut": (r.get("statut") or "").strip(),
        })
    return [l for l in lignes_dict if l["url"]]


def sauvegarder_urls(urls):
    with open(FICHIER_URLS, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "statut"])
        writer.writeheader()
        for u in urls:
            writer.writerow(u)


def ajouter_resultat(resultat):
    fichier_existe = os.path.exists(FICHIER_RESULTATS)
    with open(FICHIER_RESULTATS, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLONNES_RESULTATS)
        if not fichier_existe:
            writer.writeheader()
        writer.writerow(resultat)


# --------------------------------------------------------------------------
# Commit Git progressif (ne fait rien si on n'est pas sur GitHub Actions /
# si le dossier n'est pas un dépôt git — inoffensif en local)
# --------------------------------------------------------------------------

def commit_git(message):
    try:
        subprocess.run(["git", "add", FICHIER_URLS, FICHIER_RESULTATS], check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode == 0:
            return  # rien de nouveau à committer
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
        print(f"    💾 Commit effectué : {message}")
    except Exception as e:
        # En local (pas de dépôt git, pas de remote configuré...), on ignore.
        print(f"    (git commit ignoré : {e})")


# --------------------------------------------------------------------------
# Extraction des données sur une page Google Maps
# --------------------------------------------------------------------------

def decouper_adresse(adresse_complete):
    if not adresse_complete:
        return "", "", "", ""
    pays = "France"
    texte = adresse_complete
    match = re.search(r"(\d{5})\s+([^,]+)$", texte)
    if match:
        code_postal = match.group(1)
        ville = match.group(2).strip()
        adresse = texte[:match.start()].rstrip(", ").strip()
        return adresse, code_postal, ville, pays
    return texte, "", "", pays


async def extraire_texte(page, selecteur):
    try:
        el = await page.query_selector(selecteur)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


async def extraire_attribut(page, selecteur, attribut):
    try:
        el = await page.query_selector(selecteur)
        if el:
            return ((await el.get_attribute(attribut)) or "").strip()
    except Exception:
        pass
    return ""


async def extraire_fiche(page, url):
    nom = await extraire_texte(page, "h1.DUwDvf") or await extraire_texte(page, "h1")
    secteur = await extraire_texte(page, "button.DkEaL")
    adresse_brute = await extraire_texte(page, 'button[data-item-id="address"]')
    telephone = await extraire_texte(page, 'button[data-item-id^="phone"]')
    site_web = await extraire_attribut(page, 'a[data-item-id="authority"]', "href")

    adresse, cp, ville, pays = decouper_adresse(adresse_brute)

    return {
        "url": url,
        "nom": nom,
        "secteur_activite": secteur,
        "adresse": adresse,
        "code_postal": cp,
        "ville": ville,
        "pays": pays,
        "telephone": telephone,
        "site_web": site_web,
    }


# --------------------------------------------------------------------------
# Traitement d'une URL sur un onglet dédié
# --------------------------------------------------------------------------

async def traiter_une_url(context, entree, verrou_fichiers, etat):
    url = entree["url"]
    page = await context.new_page()

    try:
        await page.goto(url, timeout=30000)
        await page.wait_for_selector("h1", timeout=15000)
        await asyncio.sleep(random.uniform(1.0, 2.0))

        resultat = await extraire_fiche(page, url)

        if not resultat["nom"]:
            raise ValueError("Nom introuvable, la page n'a probablement pas bien chargé")

        async with verrou_fichiers:
            ajouter_resultat(resultat)
            entree["statut"] = "traité"
            sauvegarder_urls(etat["urls"])

        print(f"    ✅ {resultat['nom']}")

    except Exception as e:
        async with verrou_fichiers:
            entree["statut"] = "échec"
            sauvegarder_urls(etat["urls"])
        print(f"    ⚠️ Échec sur {url[:70]}... : {e}")

    finally:
        await page.close()

    # Commit périodique (tous les N URLs traitées, tous onglets confondus)
    async with verrou_fichiers:
        etat["compteur"] += 1
        if etat["compteur"] % GIT_COMMIT_TOUTES_LES == 0:
            commit_git(f"Avancement scraping : {etat['compteur']} URLs traitées [skip ci]")

    await asyncio.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))


# --------------------------------------------------------------------------
# Un "worker" = un onglet qui traite les URLs les unes après les autres
# --------------------------------------------------------------------------

async def worker(context, file_a_traiter, verrou_fichiers, etat, id_onglet):
    while file_a_traiter:
        try:
            entree = file_a_traiter.pop(0)
        except IndexError:
            break
        print(f"[onglet {id_onglet}] {entree['url'][:80]}...")
        await traiter_une_url(context, entree, verrou_fichiers, etat)


# --------------------------------------------------------------------------
# Boucle principale
# --------------------------------------------------------------------------

async def main():
    urls = lire_urls()
    if not urls:
        print("Aucune URL à traiter.")
        return

    a_traiter = [u for u in urls if u["statut"] not in ("traité", "échec")]
    print(f"🚀 {len(a_traiter)} URL(s) à traiter sur {len(urls)} au total.")
    print(f"   Onglets en parallèle : {NB_ONGLETS} | Pause : {PAUSE_MIN}-{PAUSE_MAX}s | "
          f"Commit tous les {GIT_COMMIT_TOUTES_LES} URLs | headless={HEADLESS}")

    if not a_traiter:
        print("Toutes les URLs ont déjà été traitées.")
        return

    verrou_fichiers = asyncio.Lock()
    etat = {"urls": urls, "compteur": 0}

    async with async_playwright() as p:
        navigateur = await p.chromium.launch(headless=HEADLESS)
        context = await navigateur.new_context(locale="fr-FR")

        taches = [
            asyncio.create_task(worker(context, a_traiter, verrou_fichiers, etat, i + 1))
            for i in range(NB_ONGLETS)
        ]
        await asyncio.gather(*taches)

        await navigateur.close()

    # Commit final pour ne pas perdre le dernier lot (< GIT_COMMIT_TOUTES_LES)
    commit_git("Mise à jour finale résultats scraping [skip ci]")

    print("\n🔒 Terminé. Résultats dans", FICHIER_RESULTATS)


if __name__ == "__main__":
    asyncio.run(main())
