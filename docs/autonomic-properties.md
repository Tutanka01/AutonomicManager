# Les 4 Propriétés Autonomiques — Comportement Détaillé

Ce document décrit le comportement exact du manager pour chacune des 4 propriétés "self-*", avec les conditions de déclenchement, les séquences d'actions et les exemples de logs.

---

## 1. Self-Configuration — Déploiement automatique

### Principe

Toute divergence entre l'**état déclaré** (`desired_state`) et l'**état réel** (Proxmox) est corrigée automatiquement. Si un service déclaré n'existe pas, il est créé.

### Condition de déclenchement

```
observed_state[vmid]["exists"] == False
```

Ce cas survient :
- Premier démarrage du manager (conteneurs pas encore créés)
- Conteneur supprimé manuellement
- VMID inexistant

### Séquence d'actions

```
MONITOR   → CT 101 : exists=False (pvesh ne le retrouve pas)
ANALYZE   → émet MISSING_SERVICE (vmid=101, property=self-configuration)
PLAN      → DEPLOY_NEW (vmid=101, hostname=wp-main, ip=192.168.100.10, template=wordpress)
EXECUTE   →
  1. pct create 101 local:vztmpl/turnkey-wordpress-... \
       --hostname wp-main --memory 512 --cores 1 \
       --rootfs local-lvm:4 \
       --net0 name=eth0,bridge=vmbr1,ip=192.168.100.10/24,gw=192.168.100.1 \
       --nameserver 8.8.8.8 --start 1
  2. sleep(10)  ← attendre le premier boot
  3. iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 8080 -j DNAT --to-destination 192.168.100.10:80
  4. iptables -t filter -A FORWARD -d 192.168.100.10 -p tcp --dport 80 -j ACCEPT
  5. KB: ip_pool.allocated["192.168.100.10"] = {vmid:101, service:"wp-main"}
  6. KB: active_port_forwarding ← ajoute la règle
```

### Logs attendus

```
[INFO]  [main]     === Cycle MAPE-K #1 ===
[INFO]  [monitor]  1 container(s) to monitor
[WARNING][monitor] CT 101 does not exist on node pve
[WARNING][analyzer] Event: MISSING_SERVICE (vmid=101, property=self-configuration)
[INFO]  [planner]  Planned action DEPLOY_NEW for CT 101 (priority=2)
[INFO]  [executor] Executing DEPLOY_NEW for CT 101
[INFO]  [proxmox]  Creating container CT 101 (hostname=wp-main, ip=192.168.100.10, template=local:vztmpl/...)
[INFO]  [proxmox]  Container CT 101 created and started successfully
[INFO]  [executor] DEPLOY_NEW: waiting 10s for CT 101 to boot
[INFO]  [network]  Added DNAT :8080 → 192.168.100.10:80 (tcp)
[INFO]  [network]  Added FORWARD ACCEPT → 192.168.100.10:80 (tcp)
[INFO]  [executor] DEPLOY_NEW: port forwarding :8080 → 192.168.100.10:80 configured
[INFO]  [executor] DEPLOY_NEW: CT 101 deployed successfully
```

### Idempotence

Si le manager est relancé et que le conteneur existe déjà, `container_exists()` retourne `True`, donc `MISSING_SERVICE` n'est pas émis. Le déploiement n'est jamais déclenché en double.

---

## 2. Self-Healing — Guérison automatique

### Principe

Un conteneur stoppé ou dont le service ne répond plus est relancé automatiquement. Si les relances échouent trop souvent, le conteneur est détruit et recréé à l'identique.

### Cas 1 : Conteneur stoppé (CONTAINER_DOWN)

**Condition :** `status == "stopped"` ET `must_be_running: true`

```
ANALYZE → CONTAINER_DOWN (vmid=101)
PLAN    → RESTART si restarts < max_restart_attempts (3)
          REDEPLOY sinon
EXECUTE (RESTART) →
  1. Vérifier cooldown (30s entre chaque tentative)
  2. pct start 101
  3. sleep(10)
  4. pvesh get /nodes/pve/lxc/101/status/current → vérifier status=="running"
  5. Health check HTTP GET http://192.168.100.10:80/
  6. Si running + healthy → reset restart_counter à 0
  7. Sinon → incrémenter restart_counter
```

**Évolution du compteur :**

| Cycle | État observé | Action planifiée | restart_counter |
|-------|-------------|-----------------|-----------------|
| N | stopped | RESTART (attempt 1/3) | 1 |
| N+1 | stopped | RESTART (attempt 2/3) | 2 |
| N+2 | stopped | RESTART (attempt 3/3) | 3 |
| N+3 | stopped | REDEPLOY | reset à 0 |

### Logs attendus (RESTART)

```
[INFO]  [monitor]  CT 101: status=stopped, CPU=0.0%, MEM=0.0%, service=DOWN
[WARNING][analyzer] Event: CONTAINER_DOWN (vmid=101, property=self-healing)
[INFO]  [planner]  Plan RESTART for CT 101 (attempt 1/3)
[INFO]  [executor] Executing RESTART for CT 101
[INFO]  [proxmox]  Starting CT 101
[INFO]  [proxmox]  CT 101 started
[INFO]  [executor] RESTART: waiting 10s for CT 101 to boot
[INFO]  [executor] RESTART: CT 101 recovered and service responding — restart counter reset
```

### Cas 2 : Service inaccessible (SERVICE_UNREACHABLE)

**Condition :** `status == "running"` mais `service_alive == False`

Scénario : le conteneur est actif mais Apache/Nginx s'est planté à l'intérieur.

```
ANALYZE → SERVICE_UNREACHABLE (vmid=101)
PLAN    → RESTART
EXECUTE → pct start 101 (tente de relancer le service via restart du conteneur)
```

### Cas 3 : Redéploiement complet (REDEPLOY)

Déclenché quand `restart_counter >= max_restart_attempts`.

```
EXECUTE (REDEPLOY) →
  1. Supprimer règles iptables existantes
  2. pct stop 101 --force
  3. pct destroy 101 --purge
  4. sleep(3)
  5. pct create 101 ... (même VMID, même IP, même config)
  6. sleep(10)
  7. Reconfigurer port forwarding
  8. reset restart_counter à 0
```

### Logs attendus (REDEPLOY)

```
[INFO]  [executor] Executing REDEPLOY for CT 101
[INFO]  [executor] REDEPLOY: destroying and recreating CT 101 (wp-main) at 192.168.100.10
[INFO]  [network]  Removing port forwarding :8080 → 192.168.100.10:80
[INFO]  [proxmox]  Stopping CT 101 before destroy
[INFO]  [proxmox]  Destroying CT 101 (purge)
[INFO]  [proxmox]  CT 101 destroyed
[INFO]  [proxmox]  Creating container CT 101 (hostname=wp-main, ip=192.168.100.10, ...)
[INFO]  [proxmox]  Container CT 101 created and started successfully
[INFO]  [executor] REDEPLOY: CT 101 redeployed successfully
```

### Cooldown

Le paramètre `restart_cooldown: 30` empêche deux tentatives de restart en moins de 30 secondes. Sans ce mécanisme, un service qui met 20 secondes à démarrer pourrait être redémarré en boucle frénétique.

---

## 3. Self-Optimization — Scaling automatique

### Principe

La charge CPU est mesurée cycle après cycle. Si elle reste haute pendant N cycles consécutifs, une réplique est créée. Si elle reste basse et des répliques existent, une réplique est supprimée.

### Scale-Out (ajout de réplique)

**Conditions :**
- `cpu_percent > cpu_scale_out` (80% par défaut)
- Pendant `sustained_cycles` cycles consécutifs (3 par défaut) → délai = 45s
- `len(replicas) < max_replicas` (3 par défaut)

```
ANALYZE → sustained_counters["101"]["high_cpu_cycles"] atteint 3
       → émet HIGH_CPU_SUSTAINED (vmid=101, cpu=85.2%)

PLAN    → allocate_vmid(kb) → 200
          allocate_ip(kb, "scaling") → "192.168.100.50"
          allocate_port(kb) → 8081
          → SCALE_OUT (parent=101, new_vmid=200, ip=.50, host_port=8081)

EXECUTE →
  1. pct create 200 local:vztmpl/turnkey-wordpress-... \
       --hostname wp-main-replica-1 \
       --net0 name=eth0,bridge=vmbr1,ip=192.168.100.50/24,gw=192.168.100.1 \
       ...
  2. sleep(10)
  3. iptables DNAT :8081 → 192.168.100.50:80
  4. KB: scaling_replicas["200"] = {vmid:200, ip:.50, parent_vmid:101, host_port:8081, ...}
  5. KB: ip_pool.allocated[".50"] = {vmid:200, pool:"scaling"}
```

Après le scale-out, les deux instances WordPress sont accessibles :
- `http://{host}:8080` → 192.168.100.10 (service principal)
- `http://{host}:8081` → 192.168.100.50 (réplique 1)

### Logs attendus (Scale-Out)

```
[INFO]  [monitor]  CT 101 (wp-main): running, CPU=87.3%, MEM=62.1%, service=UP
[WARNING][analyzer] Event: HIGH_CPU_SUSTAINED (vmid=101, cpu=87.3%, cycles=3, property=self-optimization)
[INFO]  [planner]  Plan SCALE_OUT for CT 101: new replica CT 200 at 192.168.100.50, host_port=8081
[INFO]  [executor] SCALE_OUT: deploying replica CT 200 for service CT 101 at 192.168.100.50
[INFO]  [proxmox]  Creating container CT 200 (hostname=wp-main-replica-1, ip=192.168.100.50, ...)
[INFO]  [network]  Added DNAT :8081 → 192.168.100.50:80 (tcp)
[INFO]  [executor] SCALE_OUT: replica CT 200 deployed — port :8081 → 192.168.100.50:80
```

### Scale-In (suppression de réplique)

**Conditions :**
- `cpu_percent < cpu_scale_in` (20% par défaut) pour le service principal
- Pendant `sustained_cycles` cycles consécutifs
- Au moins 1 réplique existe (on ne supprime jamais le service principal)

La **réplique la plus récente** (dernière ajoutée dans `scaling_replicas`) est choisie.

```
PLAN    → SCALE_IN (parent=101, replica_vmid=200, container_ip=.50, host_port=8081)

EXECUTE →
  1. iptables -D PREROUTING ... (supprime DNAT)
  2. iptables -D FORWARD ... (supprime ACCEPT)
  3. pct stop 200 --force ; pct destroy 200 --purge
  4. KB: del scaling_replicas["200"]
  5. KB: release_ip(kb, "192.168.100.50")
```

### Cas limite : max_replicas atteint

Si 3 répliques existent et que le CPU reste à 90%, le planner logge :

```
[INFO]  [planner]  Plan SCALE_OUT: CT 101 already at max replicas (3/3), skipping
```

Aucune action n'est générée. Le manager attend que la charge redescende.

---

## 4. Self-Protection — Isolation et remplacement

### Principe

Un conteneur au comportement anormal (CPU > 95%, flood de connexions, boucle de crash) est immédiatement :
1. Déconnecté du réseau (déplacé vers une IP de quarantaine)
2. Bloqué par iptables (DROP)
3. Remplacé par un conteneur propre à l'IP originale

### Conditions de déclenchement

| Événement | Seuil | Détection |
|-----------|-------|-----------|
| `CPU_SPIKE` | `cpu > 95%` | Instantané, 1 seul cycle suffit |
| `EXCESSIVE_CONNECTIONS` | `connections > 500` | Instantané |
| `RESTART_LOOP` | `restart_counter >= 5` | Cumulatif (pas nécessairement consécutif) |

### Scénario complet : CPU Spike

```
MONITOR   → CT 101 : running, CPU=98.5%, connections=823

ANALYZE   → CPU_SPIKE (vmid=101, cpu=98.5%)
            EXCESSIVE_CONNECTIONS (vmid=101, conn=823)
            [seul QUARANTINE généré car même vmid, priorité max]

PLAN      → allocate_ip(kb, "quarantine") → 192.168.100.200
            allocate_vmid(kb) → 200
            QUARANTINE (vmid=101, original_ip=.10, quarantine_ip=.200, new_vmid=200)

EXECUTE   →
  1. Supprime iptables DNAT :8080 → .10:80
  2. pct set 101 --net0 name=eth0,bridge=vmbr1,ip=192.168.100.200/24,gw=192.168.100.1
  3. iptables -I FORWARD 1 -d 192.168.100.200 -j DROP
  4. iptables -I FORWARD 1 -s 192.168.100.200 -j DROP
  5. KB: quarantined["101"] = {vmid:101, quarantine_ip:.200, original_ip:.10, since:now()}
  6. pct create 200 ... --net0 ...,ip=192.168.100.10/24,... (IP ORIGINALE)
  7. sleep(10)
  8. Reconfigure iptables DNAT :8080 → .10:80 (pour le nouveau CT 200)
  9. KB: desired_state.services[0].vmid = 200  (le manager suit maintenant CT 200)
 10. KB: ip_pool.allocated[".10"] = {vmid:200}
 11. KB: ip_pool.allocated[".200"] = {vmid:101, pool:"quarantine"}
```

### Logs attendus (Quarantaine)

```
[CRITICAL][analyzer] Event: CPU_SPIKE (vmid=101, cpu=98.5%, property=self-protection)
[CRITICAL][analyzer] Event: EXCESSIVE_CONNECTIONS (vmid=101, conn=823, property=self-protection)
[INFO]  [planner]  Plan QUARANTINE for CT 101: quarantine_ip=192.168.100.200, replacement CT 200 at 192.168.100.10
[WARNING][executor] QUARANTINE: isolating CT 101 → 192.168.100.200 (replacement CT 200)
[INFO]  [network]  Removing port forwarding :8080 → 192.168.100.10:80
[INFO]  [proxmox]  Changing CT 101 network to ip=192.168.100.200
[INFO]  [proxmox]  CT 101 network reconfigured to 192.168.100.200
[INFO]  [network]  Blocked traffic for IP 192.168.100.200 (rule: -d 192.168.100.200 -j DROP)
[INFO]  [network]  Blocked traffic for IP 192.168.100.200 (rule: -s 192.168.100.200 -j DROP)
[INFO]  [executor] QUARANTINE: deploying replacement CT 200 at 192.168.100.10
[INFO]  [proxmox]  Creating container CT 200 (hostname=wp-main, ip=192.168.100.10, ...)
[INFO]  [executor] QUARANTINE: waiting 10s for replacement CT 200 to boot
[INFO]  [network]  Added DNAT :8080 → 192.168.100.10:80 (tcp)
[INFO]  [executor] QUARANTINE: port forwarding :8080 → 192.168.100.10:80 restored for CT 200
[INFO]  [executor] QUARANTINE: CT 101 isolated at 192.168.100.200, replacement CT 200 active at 192.168.100.10
```

### Nettoyage automatique de la quarantaine

Après `quarantine_duration` secondes (300s = 5 minutes) :

```
ANALYZE → QUARANTINE_EXPIRED (vmid=101, quarantine_ip=.200)
PLAN    → CLEANUP_QUARANTINE
EXECUTE →
  1. iptables -D FORWARD -d .200 -j DROP
  2. iptables -D FORWARD -s .200 -j DROP
  3. pct stop 101 --force ; pct destroy 101 --purge
  4. release_ip(kb, "192.168.100.200")
  5. del quarantined["101"]
```

Le conteneur compromis est définitivement supprimé. Toute investigation forensique doit être menée **pendant** les 300 secondes de quarantaine (ou augmenter `quarantine_duration`).

### Scénario : Boucle de crash (RESTART_LOOP)

Ce cas survient quand un conteneur crashe, est redémarré, re-crashe, etc. Le compteur `restart_counters` accumule les tentatives :

- `restart_counters["101"] = 5` → `RESTART_LOOP` → `QUARANTINE`

C'est la situation où un malware ou une erreur de configuration provoque des crashs répétés. La quarantaine évite que le conteneur compromis reste en boucle indéfiniment.

---

## Comparaison des délais de réaction

| Propriété | Délai minimum | Délai par défaut |
|-----------|--------------|-----------------|
| Self-Configuration | 1 cycle = 15s | 15s |
| Self-Healing (restart) | 1 cycle + cooldown = 45s | 45s |
| Self-Optimization (scale-out) | 3 cycles = 45s | 45s |
| Self-Protection | 1 cycle = 15s | 15s (instantané) |

> Ajuster `check_interval` pour un système de production : 30-60s est réaliste. Des valeurs très basses (< 5s) peuvent surcharger l'API Proxmox.
