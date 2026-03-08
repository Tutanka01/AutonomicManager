# Architecture Technique

## 1. Vue d'ensemble de la boucle MAPE-K

La boucle **MAPE-K** est le cœur du manager autonomique. Elle s'exécute en continu avec un intervalle configurable (par défaut 15 secondes).

```
┌─────────────────────────────────────────────────────────────────────┐
│                        BOUCLE MAPE-K                                │
│                                                                     │
│  ┌──────────┐    observed     ┌──────────┐    events    ┌────────┐  │
│  │ MONITOR  │ ─────────────▶ │ ANALYZE  │ ──────────▶ │  PLAN  │  │
│  └──────────┘                └──────────┘              └────────┘  │
│       ▲                           │                        │        │
│       │                           │ (mise à jour           │actions │
│       │                           │  sustained_counters)   ▼        │
│       │                     ┌─────────────────────┐              │  │
│       │                     │    KNOWLEDGE BASE   │◀─────────────┘  │
│       │                     │    (knowledge.yaml) │                 │
│       │                     └─────────────────────┘                 │
│       │                                                   │         │
│       │                     ┌──────────┐                  │         │
│       └─────────────────────│ EXECUTE  │◀─────────────────┘         │
│         (état Proxmox réel) └──────────┘                            │
└─────────────────────────────────────────────────────────────────────┘
```

### Rôle de chaque phase

| Phase | Module | Entrée | Sortie |
|-------|--------|--------|--------|
| **Monitor** | `monitor.py` | Knowledge Base | `dict[vmid → métriques]` |
| **Analyze** | `analyzer.py` | métriques observées + KB | `list[événements]` |
| **Plan** | `planner.py` | événements + KB | `list[actions triées]` |
| **Execute** | `executor.py` | actions + KB | modifications sur Proxmox + mise à jour KB |

---

## 2. Architecture réseau

```
INTERNET / Réseau externe
       │
  [ eth0 / eno1 ]  ← interface physique du host Proxmox
       │
  ╔════════════════════════════════════╗
  ║         HOST PROXMOX VE            ║
  ║                                    ║
  ║  [ vmbr0 ]  ← bridge externe       ║
  ║     │      IP host ex: 10.0.0.10   ║
  ║     │                              ║
  ║   iptables NAT (MASQUERADE)        ║
  ║   iptables DNAT (port forwarding)  ║
  ║     │                              ║
  ║  [ vmbr1 ]  ← bridge interne isolé ║
  ║     │      IP host : 192.168.100.1 ║
  ║     │      bridge-ports none       ║
  ║     │                              ║
  ║  ┌──┼──┬──┐                        ║
  ║  CT  CT  CT ...                    ║
  ║  .10 .11 .50 (réseau 192.168.100/24)║
  ╚════════════════════════════════════╝
```

### Plages IP gérées par le manager

| Plage | Usage | YAML |
|-------|-------|------|
| `192.168.100.10 – .49` | Services déclarés dans `desired_state` | `ip_pool.service_range` |
| `192.168.100.50 – .99` | Répliques de scaling automatique | `ip_pool.scaling_range` |
| `192.168.100.200 – .254` | Conteneurs en quarantaine | `ip_pool.quarantine_range` |

### Règles iptables pré-configurées (à faire UNE FOIS manuellement)

Ces règles doivent être en place AVANT de lancer le manager :

```bash
# NAT sortant : les conteneurs peuvent accéder à internet
iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o vmbr0 -j MASQUERADE

# Permettre le transit des paquets IP
echo 1 > /proc/sys/net/ipv4/ip_forward
```

### Règles gérées DYNAMIQUEMENT par le manager

Le manager ajoute/supprime ces règles automatiquement selon les besoins :

```bash
# Port forwarding vers un conteneur (DEPLOY_NEW, QUARANTINE, REDEPLOY)
iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 8080 \
    -j DNAT --to-destination 192.168.100.10:80

iptables -t filter -A FORWARD -d 192.168.100.10 -p tcp --dport 80 -j ACCEPT

# Blocage d'un conteneur en quarantaine (QUARANTINE)
iptables -t filter -I FORWARD 1 -d 192.168.100.200 -j DROP
iptables -t filter -I FORWARD 1 -s 192.168.100.200 -j DROP
```

---

## 3. Hiérarchie des modules

```
main.py
├── monitor.py
│   ├── proxmox.py          → pvesh get /nodes/{node}/lxc
│   │                       → pvesh get /nodes/{node}/lxc/{vmid}/status/current
│   │                       → pct exec {vmid} -- ss -t state established | wc -l
│   └── knowledge.py        → lecture desired_state, templates, runtime_state
│
├── analyzer.py
│   ├── knowledge.py        → lecture thresholds, sustained_counters, quarantined
│   └── utils.py            → now_ts() pour vérifier quarantine_duration
│
├── planner.py
│   └── knowledge.py        → allocate_ip, allocate_vmid, allocate_port
│                           → get_replicas_for_parent, get_template_config
│
├── executor.py
│   ├── proxmox.py          → create_container, start_container, destroy_container
│   │                       → set_container_network, get_container_status
│   ├── network.py          → add_port_forwarding, remove_port_forwarding
│   │                       → block_ip, unblock_ip
│   ├── knowledge.py        → release_ip
│   └── utils.py            → now_ts() pour cooldowns
│
└── knowledge.py
    └── utils.py            (aucun)
```

**Règle invariante** : aucun import circulaire. Les modules "feuilles" (`proxmox`, `network`, `knowledge`, `utils`) n'importent jamais les modules "métier" (`monitor`, `analyzer`, `planner`, `executor`).

---

## 4. Flux d'exécution détaillé d'un cycle

```
t=0s   main.py démarre le cycle N
         │
t=0s   monitor(kb)
         ├── list_containers("pve")           → pvesh (JSON)
         ├── pour CT 101 :
         │   ├── container_exists("pve", 101) → bool
         │   ├── get_container_status(...)    → cpu=12%, mem=45%, status=running
         │   ├── _run_health_check(...)       → HTTP GET 192.168.100.10:80 → 200 OK
         │   └── _get_connection_count(...)   → pct exec 101 -- ss ... → 42 conns
         └── retourne { 101: { status:"running", cpu:12.3, ... } }
         │
t=1s   analyze(observed, kb)
         ├── vérifie quarantines expirées
         ├── CT 101 : cpu=12.3% → sous scale_out (80%) → high_cpu_cycles=0
         │           cpu=12.3% → au-dessus scale_in (20%) → low_cpu_cycles=0
         └── retourne [] (aucun événement)
         │
t=1s   plan([], kb) → retourne []
         │
t=1s   pas d'actions → on passe à la sauvegarde
         │
t=1s   save_kb(kb, "knowledge.yaml")
         ├── mkstemp → écrit knowledge.yaml.tmp
         └── os.replace → rename atomique
         │
t=1s   sleep(15)
t=16s  cycle N+1 commence
```

---

## 5. Gestion des priorités d'événements

Les événements sont classés par priorité croissante (0 = le plus urgent) :

```
Priorité 0 — self-protection
  CPU_SPIKE, EXCESSIVE_CONNECTIONS, RESTART_LOOP, QUARANTINE_EXPIRED

Priorité 1 — self-healing
  CONTAINER_DOWN, SERVICE_UNREACHABLE

Priorité 2 — self-configuration
  MISSING_SERVICE

Priorité 3 — self-optimization (scale-out)
  HIGH_CPU_SUSTAINED

Priorité 4 — self-optimization (scale-in)
  LOW_CPU_SUSTAINED
```

Si un même VMID génère plusieurs événements (ex: CPU_SPIKE + CONTAINER_DOWN), seule l'action la plus prioritaire est planifiée. Les événements de protection (priorité 0) sont traités en premier pour ne pas établir de règles régulières sur un conteneur compromis.

---

## 6. Persistance de l'état

La Knowledge Base (`knowledge.yaml`) est l'unique source de vérité de l'état courant. Elle est lue une fois au démarrage puis mise à jour et sauvegardée à la fin de **chaque cycle**.

### Écriture atomique

Pour éviter la corruption en cas de coupure pendant l'écriture :

```python
fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
# écriture dans tmp_path
os.replace(tmp_path, "knowledge.yaml")   # atomique sur le même filesystem
```

`os.replace` est atomique sur Linux (appel système `rename(2)`). Si le process est tué pendant l'écriture, l'ancien fichier reste intact.

### Ce qui est persisté après chaque cycle

- `runtime_state.scaling_replicas` — répliques actives
- `runtime_state.restart_counters` — compteurs de tentatives de restart
- `runtime_state.quarantined` — conteneurs en quarantaine avec timestamp
- `runtime_state.sustained_counters` — cycles CPU consécutifs
- `runtime_state.last_restart_times` — derniers instants de restart (cooldown)
- `ip_pool.allocated` — IPs actuellement attribuées
- `active_port_forwarding` — règles NAT actives
- `global.next_vmid` — prochain VMID disponible
- `port_forwarding_next_port` — prochain port host disponible
