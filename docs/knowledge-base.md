# Knowledge Base — Structure et Référence des Champs

La Knowledge Base est le fichier `knowledge.yaml` situé à la racine du projet. C'est **l'unique source de vérité** de l'état de l'infrastructure gérée. Elle est lue au démarrage et réécrite à la fin de chaque cycle MAPE-K.

---

## Structure globale

```yaml
global:          # paramètres globaux du manager
ip_pool:         # gestion des plages IP
templates:       # définitions des images TurnKey Linux
desired_state:   # état désiré de l'infrastructure (ce qui DOIT exister)
thresholds:      # seuils déclenchant les 4 propriétés autonomiques
port_forwarding_next_port: 8081
active_port_forwarding: []
runtime_state:   # état courant (mis à jour automatiquement)
```

---

## `global` — Paramètres globaux

```yaml
global:
  node_name: "pve"           # (str) Nom du node Proxmox — vérifier avec: pvesh get /nodes
  check_interval: 15         # (int) Secondes entre chaque cycle MAPE-K
  bridge_internal: "vmbr1"   # (str) Bridge réseau interne (conteneurs)
  bridge_external: "vmbr0"   # (str) Bridge réseau externe (accès internet, port forwarding)
  gateway: "192.168.100.1"   # (str) Passerelle par défaut des conteneurs
  nameserver: "8.8.8.8"      # (str) DNS des conteneurs
  next_vmid: 200             # (int) Prochain VMID pour créations dynamiques (auto-incrémenté)
```

> **Note `node_name`** : vérifier la valeur exacte avec `pvesh get /nodes`. Sur une installation standard, c'est `"pve"`. Sur un cluster multi-nœuds, adapter.

> **Note `next_vmid`** : ne pas utiliser de VMIDs déjà pris par des machines virtuelles ou conteneurs existants. Vérifier avec `pvesh get /nodes/pve/lxc` et `pvesh get /nodes/pve/qemu`.

---

## `ip_pool` — Gestion des plages IP

```yaml
ip_pool:
  service_range:
    start: 10         # 192.168.100.10
    end: 49           # 192.168.100.49
  scaling_range:
    start: 50         # 192.168.100.50
    end: 99           # 192.168.100.99
  quarantine_range:
    start: 200        # 192.168.100.200
    end: 254          # 192.168.100.254
  allocated:
    "192.168.100.10":
      vmid: 101
      service: "wp-main"
    "192.168.100.50":
      vmid: 200
      service: "wp-main-replica-1"
      pool: "scaling"
```

### Règles de séparation des plages

| Plage | Qui l'utilise | Alloué par |
|-------|--------------|------------|
| `.10 – .49` | Services de `desired_state` | définis manuellement dans `desired_state` |
| `.50 – .99` | Répliques de scaling (`SCALE_OUT`) | `allocate_ip(kb, "scaling")` |
| `.200 – .254` | Conteneurs en quarantaine | `allocate_ip(kb, "quarantine")` |

**Les plages ne se chevauchent pas** : `allocate_ip` recherche uniquement dans la plage demandée, il n'y a donc jamais de conflit.

### `ip_pool.allocated`

Dictionnaire clé=IP, valeur=métadonnées. Permet à `allocate_ip` de savoir quelles IPs sont déjà prises.

```yaml
allocated:
  "192.168.100.10":
    vmid: 101
    service: "wp-main"
  "192.168.100.50":
    vmid: 200
    service: "wp-main-replica-1"
    pool: "scaling"
  "192.168.100.200":
    vmid: 101               # VMID original, maintenant en quarantaine
    service: "quarantine-101"
    pool: "quarantine"
```

---

## `templates` — Définitions des templates TurnKey

```yaml
templates:
  wordpress:
    file: "local:vztmpl/turnkey-wordpress-18.0-bookworm-amd64.tar.gz"
    memory: 512      # (int) RAM en MiB
    cores: 1         # (int) Nombre de vCPU
    disk: "local-lvm:4"   # (str) Stockage:taille_en_Go
    health_check:
      type: "http"         # "http" ou "tcp"
      port: 80
      path: "/"            # (http seulement)
      expected_status: 200 # (http seulement)
  mysql:
    file: "local:vztmpl/turnkey-mysql-18.0-bookworm-amd64.tar.gz"
    memory: 512
    cores: 1
    disk: "local-lvm:4"
    health_check:
      type: "tcp"
      port: 3306           # pas de path/expected_status pour TCP
```

### Format du champ `disk`

Le format est `{storage_id}:{size_gb}`. Les storages disponibles sur Proxmox :
```bash
pvesh get /nodes/pve/storage  # liste les storages disponibles
```

Exemples courants :
- `local-lvm:4` — LVM thin provisioning, 4 Go
- `local:4` — répertoire local, 4 Go
- `ceph-pool:4` — Ceph RBD (si configuré)

### Health check de type `http`

Le manager fait : `GET http://{container_ip}:{port}{path}` et compare le code de retour à `expected_status`.

### Health check de type `tcp`

Le manager tente `socket.create_connection((container_ip, port), timeout)`. Succès si pas d'exception.

---

## `desired_state` — État désiré

C'est la déclaration de ce qui **doit** exister dans l'infrastructure. Le manager s'y réfère pour détecter les divergences.

```yaml
desired_state:
  services:
    - name: "wp-main"            # (str) Nom lisible, utilisé comme hostname du LXC
      template: "wordpress"      # (str) Clé dans la section templates
      vmid: 101                  # (int) VMID Proxmox fixe (mis à jour si quarantaine)
      ip: "192.168.100.10"       # (str) IP fixe du conteneur
      must_be_running: true      # (bool) Déclenche CONTAINER_DOWN si stopped
      port_forwarding:           # (optionnel) Accès depuis l'extérieur
        host_port: 8080          # Port sur l'interface externe du host Proxmox
        container_port: 80       # Port interne du service dans le conteneur
        protocol: "tcp"          # "tcp" ou "udp"
```

### Ajouter un nouveau service

Pour qu'un service soit automatiquement déployé s'il n'existe pas :

```yaml
desired_state:
  services:
    - name: "wp-main"
      template: "wordpress"
      vmid: 101
      ip: "192.168.100.10"
      must_be_running: true
      port_forwarding:
        host_port: 8080
        container_port: 80
        protocol: "tcp"

    - name: "nginx-proxy"          # ← nouveau service
      template: "nginx"
      vmid: 102
      ip: "192.168.100.11"
      must_be_running: true
      port_forwarding:
        host_port: 8090
        container_port: 80
        protocol: "tcp"

    - name: "db-main"              # ← sans accès externe
      template: "mysql"
      vmid: 103
      ip: "192.168.100.12"
      must_be_running: true
      # pas de port_forwarding → accessible uniquement depuis les autres conteneurs
```

> **Important** : si le conteneur existe déjà sur Proxmox avec ce VMID, le manager ne le recrée pas. Il le surveille et réagit s'il tombe.

---

## `thresholds` — Seuils autonomiques

### `self_optimization`

```yaml
thresholds:
  self_optimization:
    cpu_scale_out: 80        # (%) CPU moyen au-dessus duquel on ajoute une réplique
    cpu_scale_in: 20         # (%) CPU moyen en-dessous duquel on supprime une réplique
    sustained_cycles: 3      # Nombre de cycles consécutifs requis avant action
    max_replicas: 3          # Nombre maximum de répliques par service
```

Le délai effectif avant scale-out = `sustained_cycles × check_interval` = `3 × 15s = 45s`.

### `self_healing`

```yaml
thresholds:
  self_healing:
    max_restart_attempts: 3   # Après N échecs → REDEPLOY au lieu de RESTART
    health_check_timeout: 5   # Secondes d'attente pour les health checks HTTP/TCP
    restart_cooldown: 30      # Secondes minimum entre deux tentatives de restart
```

### `self_protection`

```yaml
thresholds:
  self_protection:
    max_cpu_spike: 95         # (%) CPU instantané → déclenche QUARANTINE immédiate
    max_connections: 500      # Connexions TCP établies → déclenche QUARANTINE
    max_restart_failures: 5   # Restarts cumulatifs → déclenche QUARANTINE (boucle crash)
    quarantine_duration: 300  # Secondes avant destruction auto du conteneur en quarantaine
```

---

## `port_forwarding_next_port` et `active_port_forwarding`

```yaml
port_forwarding_next_port: 8081   # Prochain port host disponible (auto-incrémenté)

active_port_forwarding:            # Règles iptables actives (géré automatiquement)
  - host_port: 8080
    container_ip: "192.168.100.10"
    container_port: 80
    protocol: "tcp"
    vmid: 101
  - host_port: 8081
    container_ip: "192.168.100.50"
    container_port: 80
    protocol: "tcp"
    vmid: 200
```

`active_port_forwarding` est la liste de toutes les règles DNAT actives. Elle est mise à jour par l'executor lors de chaque DEPLOY_NEW, SCALE_OUT, SCALE_IN, REDEPLOY, QUARANTINE.

---

## `runtime_state` — État d'exécution

Cette section est entièrement gérée par le manager. Ne jamais la modifier manuellement sauf pour une réinitialisation forcée.

### `scaling_replicas`

```yaml
runtime_state:
  scaling_replicas:
    "200":                          # clé = str(vmid de la réplique)
      vmid: 200
      ip: "192.168.100.50"
      hostname: "wp-main-replica-1"
      parent_vmid: 101              # VMID du service parent
      template: "wordpress"
      host_port: 8081
      container_port: 80
      protocol: "tcp"
    "201":
      vmid: 201
      ip: "192.168.100.51"
      hostname: "wp-main-replica-2"
      parent_vmid: 101
      template: "wordpress"
      host_port: 8082
      container_port: 80
      protocol: "tcp"
```

### `restart_counters`

```yaml
runtime_state:
  restart_counters:
    "101": 2      # CT 101 a eu 2 tentatives de restart
    "102": 0      # CT 102 n'a jamais crashé (ou a été reset)
```

Le compteur est reset à 0 après un restart **réussi** (conteneur running + health check OK) ou après un REDEPLOY.

### `quarantined`

```yaml
runtime_state:
  quarantined:
    "101":
      vmid: 101
      quarantine_ip: "192.168.100.200"
      original_ip: "192.168.100.10"
      since: 1741436400.0          # timestamp Unix UTC
```

Après `quarantine_duration` secondes, l'analyzer émet `QUARANTINE_EXPIRED` et l'executor détruit le conteneur en quarantaine.

### `sustained_counters`

```yaml
runtime_state:
  sustained_counters:
    "101":
      high_cpu_cycles: 2   # cycles consécutifs avec CPU > cpu_scale_out
      low_cpu_cycles: 0    # cycles consécutifs avec CPU < cpu_scale_in
```

### `last_restart_times`

```yaml
runtime_state:
  last_restart_times:
    "101": 1741436350.0    # timestamp Unix du dernier restart tenté
```

Utilisé pour le cooldown : le manager attend `restart_cooldown` secondes entre deux tentatives.

---

## Réinitialisation de la KB

Pour remettre l'état runtime à zéro (ex: après une intervention manuelle) :

```yaml
runtime_state:
  scaling_replicas: {}
  restart_counters: {}
  quarantined: {}
  sustained_counters: {}
  last_restart_times: {}
```

Attention : si des répliques existent sur Proxmox mais ne sont plus dans `scaling_replicas`, le manager ne les verra plus et ne les supprimera pas. Les supprimer manuellement avec `pct destroy {vmid} --purge`.
