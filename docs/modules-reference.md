# Référence des Modules Python

## `utils.py` — Utilitaires partagés

Module sans dépendances externes. Importé par tous les autres modules.

### `setup_logging(log_dir="logs") -> None`
Configure le système de logging global avec deux handlers :
- **FileHandler** → `logs/autonomic.log` (niveau DEBUG)
- **StreamHandler** → stdout (niveau INFO)

Format : `%(asctime)s [%(levelname)s] [%(module)s] %(message)s`

Appeler **une seule fois** depuis `main.py` avant tout autre import. Protégé contre les doublons (vérifie si les handlers existent déjà).

### `now_ts() -> float`
Retourne le timestamp Unix UTC courant. Utilisé pour mesurer les cooldowns et la durée de quarantaine.

### `now_iso() -> str`
Retourne l'heure UTC au format ISO-8601 (`2025-03-10T14:00:00Z`).

### `ip_in_range(ip, start, end, prefix="192.168.100") -> bool`
Vérifie si une IP appartient à une plage. Ex : `ip_in_range("192.168.100.55", 50, 99)` → `True`.

### `build_ip(last_octet, prefix="192.168.100") -> str`
Construit une IP depuis son dernier octet. Ex : `build_ip(50)` → `"192.168.100.50"`.

### `tcp_reachable(host, port, timeout=5.0) -> bool`
Tente une connexion TCP. Retourne `True` si la connexion réussit dans le délai imparti.

---

## `proxmox.py` — Wrapper Proxmox CLI

**Règle absolue** : seul ce module peut appeler `subprocess` pour des commandes `pvesh` ou `pct`. Tout autre module qui aurait besoin d'interagir avec Proxmox doit passer par ce module.

Chaque fonction :
- Utilise `subprocess.run(capture_output=True, text=True, timeout=30)`
- Logge la commande en DEBUG avant exécution
- Retourne `False` ou `None` en cas d'erreur (jamais d'exception levée)
- Logge l'erreur en ERROR si `returncode != 0`

### `list_containers(node) -> list[dict]`
```python
# Appelle : pvesh get /nodes/{node}/lxc --output-format json
# Retour : [{"vmid": "101", "status": "running", "name": "wp-main", ...}, ...]
containers = list_containers("pve")
```
Retourne une liste vide `[]` en cas d'erreur.

### `get_container_status(node, vmid) -> dict | None`
```python
# Appelle : pvesh get /nodes/{node}/lxc/{vmid}/status/current --output-format json
status = get_container_status("pve", 101)
# → {
#     "status": "running",
#     "cpu_percent": 23.4,    # cpu_raw / cores * 100
#     "mem_percent": 51.2,    # mem / maxmem * 100
#     "uptime": 86400,
#     "raw": {...}            # dictionnaire brut Proxmox
# }
```

**Calcul CPU** : Proxmox retourne `cpu` comme un ratio `0.0 – N.0` (N = nombre de cores). La conversion en % est : `cpu_raw / cores * 100`.

### `create_container(node, vmid, template_conf, hostname, ip, gateway, nameserver) -> bool`
```python
# Appelle : pct create {vmid} {template} --hostname ... --memory ... --start 1
ok = create_container(
    node="pve",
    vmid=200,
    template_conf={"file": "local:vztmpl/turnkey-wordpress-...", "memory": 512, "cores": 1, "disk": "local-lvm:4"},
    hostname="wp-main",
    ip="192.168.100.10",
    gateway="192.168.100.1",
    nameserver="8.8.8.8",
)
```
Timeout subprocess = 120s (création peut être lente si le template doit être décompressé).

### `start_container(node, vmid) -> bool`
`pct start {vmid}` — démarre un conteneur arrêté.

### `stop_container(node, vmid) -> bool`
`pct stop {vmid} --force` — arrêt brutal.

### `destroy_container(node, vmid) -> bool`
Séquence : `pct stop --force` (ignore erreurs) + 2s + `pct destroy --purge`. Le `--purge` supprime aussi les entrées dans la configuration Proxmox.

### `set_container_network(node, vmid, ip, gateway) -> bool`
```python
# pct set {vmid} --net0 name=eth0,bridge=vmbr1,ip={ip}/24,gw={gateway}
set_container_network("pve", 101, "192.168.100.200", "192.168.100.1")
```
Utilisé pour la quarantaine. Note : sur certaines versions de Proxmox, un redémarrage du conteneur peut être nécessaire pour que le changement d'IP prenne effet.

### `exec_in_container(node, vmid, command) -> str | None`
```python
# pct exec {vmid} -- {command}
out = exec_in_container("pve", 101, "sh -c ss -t state established | wc -l")
```
Retourne `None` en cas d'erreur. La commande est passée via `command.split()`.

### `container_exists(node, vmid) -> bool`
Appelle `list_containers` et cherche le VMID dans la liste. Pas d'appel API dédié.

---

## `network.py` — Gestion iptables

Toutes les fonctions vérifient l'idempotence avant d'agir (via `rule_exists`).

### `rule_exists(table, chain, rule_spec) -> bool`
```python
# iptables -t {table} -C {chain} {rule_spec}
exists = rule_exists("nat", "PREROUTING", ["-i", "vmbr0", "-p", "tcp", "--dport", "8080", "-j", "DNAT", "--to-destination", "192.168.100.10:80"])
```
`returncode == 0` → règle existe. `returncode == 1` → règle absente.

### `add_port_forwarding(ext_iface, host_port, container_ip, container_port, protocol="tcp") -> bool`
Ajoute deux règles si elles n'existent pas déjà :
1. `iptables -t nat -A PREROUTING -i {ext_iface} -p {proto} --dport {host_port} -j DNAT --to-destination {container_ip}:{container_port}`
2. `iptables -t filter -A FORWARD -d {container_ip} -p {proto} --dport {container_port} -j ACCEPT`

### `remove_port_forwarding(ext_iface, host_port, container_ip, container_port, protocol="tcp") -> bool`
Supprime les deux règles si elles existent (idem avec `-D` au lieu de `-A`).

### `block_ip(ip) -> bool`
Insère EN POSITION 1 (priorité maximale) :
- `iptables -t filter -I FORWARD 1 -d {ip} -j DROP`
- `iptables -t filter -I FORWARD 1 -s {ip} -j DROP`

L'insertion en position 1 garantit que les règles DROP sont évaluées avant tout ACCEPT existant.

### `unblock_ip(ip) -> bool`
Supprime les deux règles DROP si elles existent.

---

## `knowledge.py` — Knowledge Base

### `load_kb(path) -> dict`
```python
kb = load_kb("knowledge.yaml")
```
Charge le YAML et initialise les sous-clés manquantes avec des valeurs par défaut (`{}`, `[]`). Ne modifie pas le fichier sur disque.

### `save_kb(kb, path) -> None`
Écriture atomique : `tempfile.mkstemp` → `yaml.dump` → `os.replace`. Ne lève pas d'exception (loggue l'erreur et continue).

### `allocate_ip(kb, pool="scaling") -> str | None`
Cherche la première IP libre dans la plage `{pool}_range` de `ip_pool`. La marque comme allouée dans `ip_pool.allocated`. Retourne `None` si la plage est épuisée.

```python
ip = allocate_ip(kb, "scaling")   # → "192.168.100.50" (première libre)
ip = allocate_ip(kb, "quarantine") # → "192.168.100.200" (première libre)
```

### `release_ip(kb, ip) -> None`
Supprime l'IP de `ip_pool.allocated`. No-op si l'IP n'y est pas.

### `allocate_vmid(kb) -> int`
Retourne `global.next_vmid` et incrémente le compteur. Les VMIDs alloués dynamiquement commencent à 200 (configurable).

### `allocate_port(kb) -> int`
Retourne `port_forwarding_next_port` et incrémente. Commence à 8081.

### `find_service_by_vmid(kb, vmid) -> dict | None`
Cherche dans `desired_state.services` puis `runtime_state.scaling_replicas`. Utile pour retrouver la config d'un conteneur connu uniquement par son VMID.

### `get_template_config(kb, template_name) -> dict`
```python
tmpl = get_template_config(kb, "wordpress")
# → {"file": "local:vztmpl/...", "memory": 512, "cores": 1, "disk": "local-lvm:4", "health_check": {...}}
```
Lève `KeyError` si le template n'existe pas.

### `get_replicas_for_parent(kb, parent_vmid) -> list[dict]`
Retourne toutes les répliques dont `parent_vmid` correspond. Utile pour compter les répliques avant un scale-out.

---

## `monitor.py` — Phase Monitor

### `monitor(kb) -> dict[int, dict]`
Point d'entrée principal. Itère sur :
1. Les services de `desired_state.services`
2. Les répliques de `runtime_state.scaling_replicas`

Pour chaque conteneur, construit un dictionnaire de métriques :

```python
{
    101: {
        "exists": True,
        "status": "running",       # "running" | "stopped" | "missing" | "unknown"
        "cpu_percent": 23.4,
        "mem_percent": 51.2,
        "service_alive": True,     # résultat du health check
        "connections": 42,         # connexions TCP établies
        "is_replica": False,
        "parent_vmid": None,
    }
}
```

Si `exists == False`, status est `"missing"` et tous les champs numériques sont à 0.

### Health checks

```python
# HTTP (templates wordpress, nginx)
GET http://192.168.100.10:80/  → attend HTTP 200

# TCP (template mysql)
connect(192.168.100.10, 3306)  → succès ou échec dans timeout secondes
```

Les health checks ne font jamais planter le monitor (try/except sur chaque appel).

### Comptage des connexions

```bash
pct exec 101 -- sh -c "ss -t state established | wc -l"
# retour: "43\n"  → 43 - 1 (header) = 42 connexions
```

---

## `analyzer.py` — Phase Analyze

### `analyze(observed_state, kb) -> list[dict]`
Prend l'état observé (monitor) et la KB, retourne une liste d'événements triés par priorité.

**Structure d'un événement :**
```python
{
    "type": "CONTAINER_DOWN",          # constante EV_*
    "vmid": 101,
    "property": "self-healing",        # propriété autonomique
    # + champs contextuels selon le type
    "cpu_percent": 12.3,               # pour CPU_SPIKE, HIGH_CPU_SUSTAINED
    "connections": 823,                # pour EXCESSIVE_CONNECTIONS
    "restart_count": 5,                # pour RESTART_LOOP
}
```

**Types d'événements :**

| Constante | Condition | Priorité |
|-----------|-----------|----------|
| `EV_MISSING_SERVICE` | `exists == False` pour un service désiré | 2 |
| `EV_CONTAINER_DOWN` | `status == "stopped"` et `must_be_running: true` | 1 |
| `EV_SERVICE_UNREACHABLE` | `status == "running"` mais `service_alive == False` | 1 |
| `EV_HIGH_CPU_SUSTAINED` | `cpu > cpu_scale_out` pendant N cycles consécutifs | 3 |
| `EV_LOW_CPU_SUSTAINED` | `cpu < cpu_scale_in` pendant N cycles ET répliques > 0 | 4 |
| `EV_CPU_SPIKE` | `cpu > max_cpu_spike` (instantané) | 0 |
| `EV_EXCESSIVE_CONNECTIONS` | `connections > max_connections` | 0 |
| `EV_RESTART_LOOP` | counter restarts ≥ `max_restart_failures` | 0 |
| `EV_QUARANTINE_EXPIRED` | `now - quarantine.since >= quarantine_duration` | 0 |

**Logique des sustained_counters :**
```
cycle 1 : cpu=85% → high_cpu_cycles=1 (seuil=80%)
cycle 2 : cpu=82% → high_cpu_cycles=2
cycle 3 : cpu=90% → high_cpu_cycles=3 == sustained_cycles → émet HIGH_CPU_SUSTAINED, reset à 0
cycle 4 : cpu=88% → high_cpu_cycles=1 (pas d'émission)
```

---

## `planner.py` — Phase Plan

### `plan(events, kb) -> list[dict]`
Convertit chaque événement en action, alloue les ressources nécessaires, trie par priorité.

**Structure d'une action :**
```python
{
    "action": "DEPLOY_NEW",    # nom de l'action
    "priority": 2,             # PRIO_SELF_CONFIGURATION
    "vmid": 101,
    "hostname": "wp-main",
    "ip": "192.168.100.10",
    "template": "wordpress",
    "template_conf": {...},
    "port_forwarding": {"host_port": 8080, "container_port": 80, "protocol": "tcp"},
}
```

**Actions possibles :**

| Action | Événement source | Ressources allouées |
|--------|-----------------|---------------------|
| `DEPLOY_NEW` | MISSING_SERVICE | — |
| `RESTART` | CONTAINER_DOWN, SERVICE_UNREACHABLE | — |
| `REDEPLOY` | CONTAINER_DOWN (restarts épuisés) | — |
| `SCALE_OUT` | HIGH_CPU_SUSTAINED | VMID + IP scaling + port |
| `SCALE_IN` | LOW_CPU_SUSTAINED | — |
| `QUARANTINE` | CPU_SPIKE, EXCESSIVE_CONNECTIONS, RESTART_LOOP | VMID + IP quarantaine |
| `CLEANUP_QUARANTINE` | QUARANTINE_EXPIRED | — |

---

## `executor.py` — Phase Execute

### `execute(actions, kb) -> None`
Exécute chaque action dans l'ordre. Les actions sont déjà triées par priorité depuis `planner.py`.

En cas d'erreur sur une action, elle est loggée et l'exécution **continue** avec l'action suivante.

**Mises à jour KB après chaque action :**

| Action | Clés KB mises à jour |
|--------|---------------------|
| `DEPLOY_NEW` | `ip_pool.allocated`, `active_port_forwarding` |
| `RESTART` | `runtime_state.restart_counters`, `runtime_state.last_restart_times` |
| `REDEPLOY` | `runtime_state.restart_counters`, `ip_pool.allocated`, `active_port_forwarding` |
| `SCALE_OUT` | `runtime_state.scaling_replicas`, `ip_pool.allocated`, `active_port_forwarding` |
| `SCALE_IN` | `runtime_state.scaling_replicas`, `ip_pool.allocated`, `active_port_forwarding` |
| `QUARANTINE` | `runtime_state.quarantined`, `desired_state.services[].vmid`, `ip_pool.allocated` |
| `CLEANUP_QUARANTINE` | `runtime_state.quarantined`, `ip_pool.allocated` |

---

## `main.py` — Point d'entrée

```python
python3 main.py
```

Cycle typique (pseudocode) :
```
setup_logging()
kb = load_kb("knowledge.yaml")

while True:
    observed = monitor(kb)
    events   = analyze(observed, kb)
    actions  = plan(events, kb)
    if actions:
        execute(actions, kb)
    save_kb(kb, "knowledge.yaml")
    sleep(kb["global"]["check_interval"])   # défaut: 15s
```

Chaque phase est encapsulée dans un try/except : si une phase crashe, les phases suivantes s'exécutent tout de même et la KB est sauvegardée.
