# PBS on Google Cloud Platform

## 1. ネットワークの設定

### 1.1. パラメタの設定

プロジェクト ID や SSH 接続元の IP レンジを指定します。

```sh
export project_id=
export ssh_ip_range=0.0.0.0/0
export gcp_region=asia-northeast1
export gcp_zone=asia-northeast1-c
```

### 1.2. CLI の初期値設定

```bash
gcloud config set project "${project_id}"
gcloud services enable compute.googleapis.com deploymentmanager.googleapis.com
gcloud config set compute/region "${gcp_region}"
gcloud config set compute/zone "${gcp_zone}"
```

### 1.3. VPC の構築

```sh
gcloud compute networks create hpc-vpc --subnet-mode=custom
gcloud compute networks subnets create hpc-tokyo --range=10.128.0.0/16 \
    --network=hpc-vpc --enable-private-ip-google-access
gcloud compute firewall-rules create allow-from-iap \
    --network=hpc-vpc --direction=INGRESS --priority=1000 --action=ALLOW \
    --rules=tcp:22,icmp --source-ranges=35.235.240.0/20
gcloud compute firewall-rules create allow-from-internal \
    --network=hpc-vpc --direction=INGRESS --priority=1000 --action=ALLOW \
    --rules=tcp:0-65535,udp:0-65535,icmp --source-ranges=10.0.0.0/8
gcloud compute firewall-rules create allow-from-corp \
    --network=hpc-vpc --direction=INGRESS --action=ALLOW --priority=1000 \
    --rules=tcp:22,icmp --source-ranges="${ssh_ip_range}"
```

## 2. クラスタの構築

### 2.1. テンプレートのダウンロード

```sh
git clone git@github.com:pottava/pbs-on-googlecloud.git
cd ~/pbs-on-googlecloud/dm
```

### 2.2. Deployment Manager でのクラスタ構築

```sh
gcloud deployment-manager deployments create hpc-cluster --config slurm-cluster.yaml
```

## 3. ノード・PBS の設定

本来 Google Cloud では、[OS Login](https://cloud.google.com/compute/docs/instances/managing-instance-access?hl=ja) という、より安全な SSH アクセスを実現する手段がありますが  
以下では gcloud コマンドの使えないような環境から SSH する状況を想定し、設定を進めます。

### 3.1. ログインサーバへ SSH 鍵を転送

公開鍵の冒頭にユーザー名を挿入したファイルを、ログインサーバーのメタデータとして登録します。

```sh
ssh-keygen -t rsa -f id_rsa -C $(whoami)
sed "s/ssh-rsa/$(whoami):ssh-rsa/" id_rsa.pub > ssh-metadata
gcloud compute instances add-metadata hpc-login0 --zone "${gcp_zone}" \
    --metadata-from-file ssh-keys=ssh-metadata
```

### 3.2. SSH 接続

```sh
login_vm=$( gcloud compute instances describe hpc-login0 --zone "${gcp_zone}" \
    --format 'value(networkInterfaces[0].accessConfigs[0].natIP)')
ssh -i id_rsa "$(whoami)@${login_vm}"
```

## 9. リソースの削除

クラスタを停止し、

```sh
gcloud deployment-manager deployments delete hpc-cluster
```

その基礎となるリソースも削除していきます。

```sh
gcloud compute firewall-rules delete allow-from-iap --quiet
gcloud compute firewall-rules delete allow-from-internal --quiet
gcloud compute firewall-rules delete allow-from-corp --quiet
gcloud compute networks subnets delete hpc-tokyo --region "${gcp_region}" --quiet
gcloud compute networks delete hpc-vpc --quiet
```
