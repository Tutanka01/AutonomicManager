# Guide de Déploiement sur Proxmox VE

Ce document est le guide **pas-à-pas** pour installer et utiliser le manager autonomique MAPE-K sur un host Proxmox VE. Il couvre la configuration réseau initiale, l'installation du manager, sa configuration, et des scénarios de test concrets.

---

## Prérequis

### Système

| Composant | Version requise |
|-----------|----------------|
| Proxmox VE | 7.x ou 8.x |
| Python | 3.10+ (inclus sur Proxmox Bookworm) |
| Accès | Root sur le host Proxmox |

### Vérifications préalables

```bash
# 1. Version Proxmox
pveversion

# 2. Version Python
python3 --version

# 3. pvesh disponible
pvesh get /nodes --output-format json

# 4. pct disponible
pct list

# 5. iptables disponible
iptables --version
```

---

## Étape 1 — Télécharger les templates TurnKey Linux

Les templates doivent être disponibles dans le stockage `local` avant de lancer le manager.

```bash
# Lister les templates disponibles dans le catalogue TurnKey
pveam update
pveam available --section turnkeylinux

# Télécharger les templates utilisés dans knowledge.yaml
pveam download local debian-12-turnkey-wordpress_18.2-1_amd64.tar.gz
pveam download local debian-12-turnkey-nginx-php-fastcgi_18.0-1_amd64.tar.gz
pveam download local debian-12-turnkey-mysql_18.1-1_amd64.tar.gz

# Vérifier qu'ils sont bien présents
pveam list local
```

Résultat attendu :
```
NAME                                                         SIZE
local:vztmpl/debian-12-turnkey-mysql_18.1-1_amd64.tar.gz     267.84MB
local:vztmpl/debian-12-turnkey-nginx-php-fastcgi_18.0-1_amd64.tar.gz 275.22MB
local:vztmpl/debian-12-turnkey-wordpress_18.2-1_amd64.tar.gz 346.78MB
```

---

## Étape 2 — Configurer le réseau

### Créer le bridge interne `vmbr1`

Éditer `/etc/network/interfaces` et ajouter :

```
# Bridge interne isolé — pour les conteneurs MAPE-K
auto vmbr1
iface vmbr1 inet static
    address 192.168.100.1/24
    bridge-ports none
    bridge-stp off
    bridge-fd 0
```

Appliquer sans redémarrer :

```bash
ifup vmbr1
ip addr show vmbr1
# doit afficher : inet 192.168.100.1/24
```

### Activer le forwarding IP (permanent)

```bash
# Immédiat
echo 1 > /proc/sys/net/ipv4/ip_forward

# Permanent (survit aux reboots) — ajouter dans /etc/sysctl.conf
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
sysctl -p
```

### Configurer le NAT sortant

```bash
# Remplacer eno1/vmbr0 par votre interface externe réelle
EXTERNAL_IF="vmbr0"   # ou eno1 selon votre config

iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o ${EXTERNAL_IF} -j MASQUERADE

# Rendre la règle persistante (Debian/Proxmox)
apt-get install -y iptables-persistent
iptables-save > /etc/iptables/rules.v4
```

### Vérifier le nom de l'interface externe

```bash
ip route show default
# default via 10.0.0.1 dev eno1 ...
#                           ^^^^ utiliser ce nom dans knowledge.yaml → global.bridge_external
```

---

## Étape 3 — Installer le manager

```bash
# Créer le répertoire de travail
mkdir -p /opt/autonomic-manager
cd /opt/autonomic-manager

# Copier les fichiers du projet (adapter selon votre source)
# Option A : depuis une clé USB / partage réseau
cp -r /path/to/project/* /opt/autonomic-manager/

# Option B : depuis Git
git clone https://github.com/Tutanka01/AutonomicManager /opt/autonomic-manager

# Installer les dépendances Python
pip3 install -r requirements.txt

# Vérifier l'installation
python3 -c "import yaml, requests; print('OK')"
```

---

## Étape 4 — Configurer `knowledge.yaml`

Ouvrir le fichier et adapter les paramètres à votre environnement :

```bash
nano /opt/autonomic-manager/knowledge.yaml
```

### Points critiques à vérifier

```yaml
global:
  node_name: "pxmx-1"       # ← vérifier avec: pvesh get /nodes | python3 -c "import sys,json;[print(n['node']) for n in json.load(sys.stdin)]"
  bridge_external: "vmbr0"   # ← vérifier avec: ip route show default

templates:
  wordpress:
    # Vérifier que ce path correspond exactement au nom du template téléchargé :
    file: "local:vztmpl/debian-12-turnkey-wordpress_18.2-1_amd64.tar.gz"
    disk: "local-lvm:4"      # ← adapter si vous n'avez pas local-lvm

desired_state:
  services:
    - name: "wp-main"
      vmid: 101              # ← choisir un VMID non utilisé
      ip: "192.168.100.10"   # ← dans la plage .10-.49
```

### Vérifier les VMIDs libres

```bash
# Lister tous les VMIDs utilisés (VMs + LXC)
pvesh get /nodes/pve/lxc --output-format json | python3 -c "import sys,json; [print(c['vmid']) for c in json.load(sys.stdin)]"
pvesh get /nodes/pve/qemu --output-format json | python3 -c "import sys,json; [print(c['vmid']) for c in json.load(sys.stdin)]"
```

### Vérifier que le stockage existe

```bash
pvesh get /nodes/pve/storage --output-format json | python3 -c "import sys,json; [print(s['storage']) for s in json.load(sys.stdin)]"
# affiche : local, local-lvm, ...
```

---

## Étape 5 — Premier démarrage

```bash
cd /opt/autonomic-manager

# Démarrage en avant-plan pour voir les logs (première fois)
python3 main.py
```

### Sortie attendue au premier cycle

```
2026-03-08 10:00:00 [INFO] [main] Autonomic MAPE-K Manager starting
2026-03-08 10:00:00 [INFO] [main] Knowledge base loaded — node=pxmx-1, interval=15s
2026-03-08 10:00:00 [INFO] [main] === Cycle MAPE-K #1 ===
2026-03-08 10:00:00 [INFO] [monitor] 1 container(s) to monitor
2026-03-08 10:00:00 [WARNING] [monitor] CT 101 does not exist on node pxmx-1
2026-03-08 10:00:00 [WARNING] [analyzer] Event: MISSING_SERVICE (vmid=101, property=self-configuration)
2026-03-08 10:00:00 [INFO] [planner] Planned action DEPLOY_NEW for CT 101 (priority=2)
2026-03-08 10:00:00 [INFO] [executor] Executing DEPLOY_NEW for CT 101
2026-03-08 10:00:00 [INFO] [proxmox] Creating container CT 101 (hostname=wp-main, ip=192.168.100.10, ...)
2026-03-08 10:00:15 [INFO] [proxmox] Container CT 101 created and started successfully
2026-03-08 10:00:15 [INFO] [executor] DEPLOY_NEW: waiting 10s for CT 101 to boot
2026-03-08 10:00:25 [INFO] [network] Added DNAT :8080 → 192.168.100.10:80 (tcp)
2026-03-08 10:00:25 [INFO] [executor] DEPLOY_NEW: CT 101 deployed successfully
2026-03-08 10:00:25 [INFO] [main] Knowledge base saved
2026-03-08 10:00:25 [INFO] [main] Next cycle in 15s
```

### Vérifications après le premier cycle

```bash
# 1. Le conteneur est créé et actif
pct list
# VMID  Status  Name       Mem(MB)
# 101   running wp-main    512

# 2. Il répond au ping depuis le host
ping -c 3 192.168.100.10

# 3. Le service WordPress est accessible
curl -I http://192.168.100.10:80/
# HTTP/1.1 200 OK

# 4. Le port forwarding est actif
iptables -t nat -L PREROUTING -n --line-numbers | grep 8080
# DNAT  tcp  --  *  vmbr0  0.0.0.0/0  0.0.0.0/0  tcp dpt:8080 to:192.168.100.10:80

# 5. Accès depuis l'extérieur (remplacer HOST_IP par l'IP externe de votre host)
curl -I http://HOST_IP:8080/
```

---

## Étape 6 — Lancer en service systemd (production)

```bash
cat > /etc/systemd/system/autonomic-manager.service << 'EOF'
[Unit]
Description=Autonomic MAPE-K Manager for Proxmox VE
After=network.target pve-cluster.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/autonomic-manager
ExecStart=/usr/bin/python3 /opt/autonomic-manager/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable autonomic-manager
systemctl start autonomic-manager

# Vérifier le statut
systemctl status autonomic-manager

# Voir les logs en temps réel
journalctl -u autonomic-manager -f

# Ou depuis le fichier de log
tail -f /opt/autonomic-manager/logs/autonomic.log
```

---

## Scénarios de test

### Test 1 — Self-Configuration : déploiement automatique

```bash
# Vérifier qu'aucun conteneur 101 n'existe
pct list | grep 101

# Lancer le manager
python3 main.py

# Observer : CT 101 (wp-main) est créé automatiquement dans les 30 premières secondes
pct list
# → 101 running wp-main

# Vérifier l'accès
curl -sI http://192.168.100.10/ | head -1
# → HTTP/1.1 200 OK
```

---

### Test 2 — Self-Healing : redémarrage après crash

```bash
# Avec le manager actif, stopper manuellement le conteneur
pct stop 101 --force

# Observer les logs :
tail -f /opt/autonomic-manager/logs/autonomic.log
# [WARNING] CT 101: status=stopped
# [WARNING] Event: CONTAINER_DOWN ...
# [INFO] Executing RESTART for CT 101 ...
# [INFO] CT 101 started
# [INFO] CT 101 recovered and service responding

# Après ~30-45 secondes, vérifier :
pct list | grep 101
# 101 running wp-main
```

**Cas avancé : tester le REDEPLOY**

```bash
# Forcer 3 crashes successifs pour épuiser les tentatives de restart
for i in 1 2 3; do
    pct stop 101 --force
    sleep 50   # attendre que le manager tente de redémarrer et attendre le cooldown
done

# Observer : après 3 tentatives → REDEPLOY
tail -f /opt/autonomic-manager/logs/autonomic.log | grep REDEPLOY
# [INFO] Plan REDEPLOY for CT 101 (restarts=3, exceeds max=3)
# [INFO] REDEPLOY: destroying and recreating CT 101
```

---

### Test 3 — Self-Optimization : scale-out via charge CPU

Pour simuler une charge CPU élevée dans le conteneur WordPress :

```bash
# Dans le conteneur, générer de la charge CPU
pct exec 101 -- apt-get install -y stress-ng
pct exec 101 -- stress-ng --cpu 1 --cpu-load 90 --timeout 120s &

# Observer les logs (après 3 cycles × 15s = 45s minimum)
tail -f /opt/autonomic-manager/logs/autonomic.log
# [WARNING] Event: HIGH_CPU_SUSTAINED (vmid=101, cpu=90.2%, cycles=3)
# [INFO] Plan SCALE_OUT for CT 101: new replica CT 200 at 192.168.100.50
# [INFO] SCALE_OUT: deploying replica CT 200 ...

# Vérifier la réplique
pct list
# 101 running wp-main
# 200 running wp-main-replica-1

# Vérifier le second port forwarding
iptables -t nat -L PREROUTING -n | grep 8081
# → DNAT :8081 → 192.168.100.50:80

# Arrêter la charge artificielle
pct exec 101 -- killall stress-ng

# Attendre : après 3 cycles avec CPU < 20% (et si réplique existante)
# → Scale-IN automatique
tail -f /opt/autonomic-manager/logs/autonomic.log | grep SCALE_IN
```

---

### Test 4 — Self-Protection : quarantaine

**Simulation d'un CPU spike (méthode rapide pour les tests) :**

```bash
# Modifier TEMPORAIREMENT le seuil dans knowledge.yaml pour faciliter le test
# max_cpu_spike: 95 → max_cpu_spike: 30
nano /opt/autonomic-manager/knowledge.yaml
# Modifier la ligne : max_cpu_spike: 30

# Générer de la charge dans le conteneur
pct exec 101 -- stress-ng --cpu 1 --cpu-load 50 --timeout 60s &

# Observer la quarantaine dans les logs
tail -f /opt/autonomic-manager/logs/autonomic.log
# [CRITICAL] Event: CPU_SPIKE (vmid=101, cpu=48.5%)
# [WARNING] QUARANTINE: isolating CT 101 → 192.168.100.200 (replacement CT 200)
# ...
# [INFO] QUARANTINE: complete — CT 101 isolated at .200, replacement CT 200 active at .10

# Vérifications
pct list
# 101 running  (en quarantaine à .200)
# 200 running  (nouveau conteneur propre à .10)

# Vérifier le blocage iptables
iptables -L FORWARD -n | grep 192.168.100.200
# DROP  all  --  0.0.0.0/0  192.168.100.200
# DROP  all  --  192.168.100.200  0.0.0.0/0

# Vérifier que le service est toujours accessible
curl -I http://HOST_IP:8080/
# → HTTP/1.1 200 OK   (via le CT 200 propre)

# Après 300s (5 min) → nettoyage automatique
tail -f /opt/autonomic-manager/logs/autonomic.log | grep CLEANUP_QUARANTINE

# Remettre le seuil d'origine
# max_cpu_spike: 95
```

---

### Test 5 — Idempotence : redémarrage du manager

```bash
# Avec le manager actif et CT 101 running
systemctl stop autonomic-manager

# Relancer le manager
systemctl start autonomic-manager

# Observer : le manager NE recrée PAS CT 101 (il existe déjà)
tail -f /opt/autonomic-manager/logs/autonomic.log
# [INFO] CT 101: running, CPU=X%, MEM=Y%, service=UP
# [INFO] No events detected — system nominal
```

---

## Configuration avancée

### Ajouter un second service (ex: base de données MySQL)

Éditer `knowledge.yaml` :

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

    - name: "db-main"            # ← ajouter ce bloc
      template: "mysql"
      vmid: 102
      ip: "192.168.100.11"
      must_be_running: true
      # Pas de port_forwarding : MySQL accessible uniquement depuis vmbr1
```

Au prochain cycle, le manager crée CT 102 automatiquement.

### Modifier les seuils de scaling à chaud

Le manager **relit automatiquement** `knowledge.yaml` au début et à la fin de chaque cycle sans qu'il soit nécessaire de le redémarrer. Les modifications suivantes sont prises en compte immédiatement :

- `thresholds` — tous les seuils (CPU, restarts, quarantaine…)
- `global` — `check_interval`, `node_name`, bridges
- `desired_state` — ajout ou suppression de services
- `templates` — définitions des templates LXC

Il suffit d'éditer le fichier et de le sauvegarder. Au prochain cycle le manager logge :
```
[INFO] [knowledge] Config hot-reloaded from disk: thresholds
```

> **Note :** `runtime_state`, `active_port_forwarding` et les compteurs de ports ne sont jamais écrasés par le hot-reload — l'état courant en mémoire est toujours préservé.

### Surveiller les logs

```bash
# Logs en temps réel (console)
journalctl -u autonomic-manager -f

# Logs détaillés (fichier, niveau DEBUG)
tail -f /opt/autonomic-manager/logs/autonomic.log

# Filtrer par propriété
grep "self-protection" /opt/autonomic-manager/logs/autonomic.log
grep "QUARANTINE" /opt/autonomic-manager/logs/autonomic.log
grep "SCALE_OUT\|SCALE_IN" /opt/autonomic-manager/logs/autonomic.log

# Compter les événements par type
grep "Event:" /opt/autonomic-manager/logs/autonomic.log | grep -oP 'Event: \w+' | sort | uniq -c
```

---

## Maintenance

### Arrêter le manager proprement

```bash
systemctl stop autonomic-manager
# Le manager termine le cycle courant avant de s'arrêter (signal SIGTERM)
```

### Inspecter l'état courant

```bash
# Voir la KB telle qu'elle est persistée
cat /opt/autonomic-manager/knowledge.yaml

# Voir les répliques actives
python3 -c "
import yaml
with open('/opt/autonomic-manager/knowledge.yaml') as f:
    kb = yaml.safe_load(f)
replicas = kb['runtime_state']['scaling_replicas']
print(f'{len(replicas)} replica(s):')
for k,v in replicas.items():
    print(f'  CT {v[\"vmid\"]} at {v[\"ip\"]} (parent={v[\"parent_vmid\"]})')
"

# Voir les conteneurs en quarantaine
python3 -c "
import yaml, datetime
with open('/opt/autonomic-manager/knowledge.yaml') as f:
    kb = yaml.safe_load(f)
quarantined = kb['runtime_state']['quarantined']
for k,v in quarantined.items():
    since = datetime.datetime.utcfromtimestamp(v['since'])
    print(f'CT {v[\"vmid\"]} at {v[\"quarantine_ip\"]} since {since}')
"
```

### Nettoyage complet (réinitialisation)

```bash
# ATTENTION : ceci supprime tous les conteneurs gérés !
systemctl stop autonomic-manager

# Supprimer les conteneurs
for vmid in 101 102 200 201 202; do
    pct destroy ${vmid} --purge 2>/dev/null || true
done

# Vider les règles iptables dynamiques
iptables -t nat -F PREROUTING
iptables -F FORWARD
# Remettre la règle MASQUERADE
iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o vmbr0 -j MASQUERADE

# Réinitialiser la KB
cp /opt/autonomic-manager/knowledge.yaml.original /opt/autonomic-manager/knowledge.yaml
# ou éditer manuellement pour vider runtime_state et ip_pool.allocated

systemctl start autonomic-manager
```

---

## Récapitulatif des ports et IPs

| Élément | Valeur |
|---------|--------|
| Host Proxmox — vmbr1 | `192.168.100.1` |
| Service wp-main | `192.168.100.10` |
| Port externe wp-main | `:8080` → `192.168.100.10:80` |
| Répliques (auto) | `192.168.100.50 – .99` |
| Ports répliques (auto) | `8081, 8082, 8083, ...` |
| Quarantaine (auto) | `192.168.100.200 – .254` |
