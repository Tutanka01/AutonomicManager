# Projet Cloud Computing 2 — Manager Autonomique MAPE-K

Projet universitaire réalisé dans le cadre du module **Cloud Computing 2**  
**Master 1 Informatique — Université de Pau et des Pays de l'Adour (UPPA)**

---

## Description

Implémentation d'un manager autonomique basé sur la boucle de contrôle **MAPE-K** (Monitor, Analyze, Plan, Execute) pour la gestion automatisée de conteneurs LXC sur **Proxmox VE**.

Le système assure les quatre propriétés d'auto-gestion :

- **Self-Configuration** — déploiement automatique des services manquants
- **Self-Healing** — détection et récupération des pannes
- **Self-Optimization** — mise à l'échelle dynamique selon la charge
- **Self-Protection** — isolation des conteneurs compromis (quarantaine)

## Structure

```
autonomic-manager/
├── main.py           # Point d'entrée — boucle MAPE-K
├── monitor.py        # Phase Monitor
├── analyzer.py       # Phase Analyze
├── planner.py        # Phase Plan
├── executor.py       # Phase Execute
├── knowledge.py      # Base de connaissances (YAML)
├── proxmox.py        # Interface Proxmox (pvesh / pct)
├── network.py        # Gestion réseau (iptables)
├── utils.py          # Utilitaires partagés
├── knowledge.yaml    # Base de connaissances persistante
├── requirements.txt
└── docs/             # Documentation complète
```

## Documentation

Voir le dossier [`autonomic-manager/docs/`](autonomic-manager/docs/README.md) pour la documentation complète, incluant le guide de déploiement sur Proxmox.
