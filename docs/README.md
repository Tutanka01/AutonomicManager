# Manager Autonomique MAPE-K pour Proxmox VE

## Vue d'ensemble

Ce projet implémente un **système autonomique** en Python qui surveille et gère automatiquement des conteneurs LXC déployés sur un hyperviseur **Proxmox VE**. Il s'appuie sur la boucle de contrôle **MAPE-K** (Monitor, Analyze, Plan, Execute, Knowledge Base), le modèle de référence de l'informatique autonomique défini par IBM.

Le manager tourne **directement sur le host Proxmox VE** (pas dans un conteneur) et interagit avec la plateforme via les outils natifs `pvesh` et `pct` — sans bibliothèque tierce, sans API REST, sans gestion de token.

---

## Pourquoi un manager autonomique ?

Dans un datacenter traditionnel, un opérateur doit :
- Surveiller manuellement les métriques (CPU, RAM, disponibilité des services)
- Réagir aux pannes (redémarrage, redéploiement)
- Dimensionner les ressources à la main (scale-out / scale-in)
- Isoler les conteneurs compromis

Un manager autonomique automatise intégralement ces tâches, grâce aux **4 propriétés "self-*"** :

| Propriété | Ce que fait le manager |
|-----------|------------------------|
| **Self-Configuration** | Déploie automatiquement un service absent de l'infrastructure |
| **Self-Healing** | Redémarre ou redéploie un conteneur tombé ou inaccessible |
| **Self-Optimization** | Ajoute ou supprime des répliques selon la charge CPU |
| **Self-Protection** | Isole en quarantaine un conteneur au comportement anormal |

---

## Structure du projet

```
autonomic-manager/
├── main.py                  Point d'entrée — boucle MAPE-K
├── monitor.py               Phase Monitor : collecte des métriques
├── analyzer.py              Phase Analyze : détection des anomalies
├── planner.py               Phase Plan    : génération des actions
├── executor.py              Phase Execute : application des actions
├── knowledge.py             Gestion de la Knowledge Base (YAML)
├── proxmox.py               Wrapper CLI pvesh + pct
├── network.py               Gestion iptables (NAT, forwarding, blocage)
├── utils.py                 Logging, timestamps, helpers IP
├── knowledge.yaml           Knowledge Base persistante
├── requirements.txt         Dépendances Python (pyyaml, requests)
└── docs/
    ├── README.md            Ce fichier
    ├── architecture.md      Architecture technique détaillée
    ├── modules-reference.md Référence complète de chaque module
    ├── knowledge-base.md    Structure et champs de la Knowledge Base
    ├── autonomic-properties.md Les 4 propriétés self-* en détail
    ├── deployment.md        Guide de déploiement sur Proxmox VE
    └── troubleshooting.md   Résolution des problèmes courants
```

---

## Démarrage rapide

```bash
# 1. Cloner / copier le projet sur le host Proxmox
cd /opt
git clone <repo> autonomic-manager
cd autonomic-manager

# 2. Installer les dépendances Python
pip3 install -r requirements.txt

# 3. Adapter knowledge.yaml à votre configuration
nano knowledge.yaml

# 4. Lancer le manager (en root)
python3 main.py
```

> Pour un guide de déploiement complet avec exemples, voir [docs/deployment.md](deployment.md).

---

## Documentation

| Document | Contenu |
|----------|---------|
| [architecture.md](architecture.md) | Schéma réseau, flux MAPE-K, diagrammes |
| [modules-reference.md](modules-reference.md) | API de chaque module Python |
| [knowledge-base.md](knowledge-base.md) | Tous les champs YAML expliqués |
| [autonomic-properties.md](autonomic-properties.md) | Comportement détaillé des 4 self-* |
| [deployment.md](deployment.md) | Installation, configuration, exemples |
| [troubleshooting.md](troubleshooting.md) | Problèmes fréquents et solutions |

---

## Prérequis

- **Proxmox VE 7.x ou 8.x** avec accès root au shell
- **Python 3.10+** (préinstallé sur Proxmox Debian Bookworm)
- `pvesh` et `pct` disponibles (inclus dans Proxmox)
- `iptables` disponible (inclus dans Proxmox)
- Templates TurnKey Linux téléchargés dans le stockage local
- Bridge `vmbr1` configuré comme réseau interne isolé

---

## Dépendances Python

```
pyyaml>=6.0     — lecture/écriture de la Knowledge Base
requests>=2.28  — health checks HTTP
```

Toutes les autres fonctionnalités utilisent la bibliothèque standard Python 3.
