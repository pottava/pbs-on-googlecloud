# PBS on Google Cloud

PBS と NFS ベースのクラスタを GCE 上に作成します。構築にはおよそ 30 分かかります。

完成後、起動する VM は以下のような構成となります。

- login0: 外部 IP アドレスを持ったログインノード（`login_node_count` は 1 のみをサポートしています）
- controller: PBS & NFS サーバー
- compute-x-x: `static_node_count` 台の計算ノード

NFS マウントされる共有ディレクトリは以下のとおりです。

- /home: ユーザディレクトリ、個人ごとの入出力データを扱えます
- /apps: (`controller_disk_size_gb` - 3) GB 程度の容量をもつアプリケーション ディレクトリ
- /mnt/disks/sec/apps: `controller_secondary_disk_size_gb` GB の容量をもつアプリケーション ディレクトリ

クラスタ構築後、アプリケーション ディレクトリに依存ライブラリを保存し  
ジョブ起動スクリプトから Lmod や環境変数 PATH を使って参照してください。

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
gcloud compute firewall-rules create allow-from-internal \
    --network=hpc-vpc --direction=INGRESS --priority=1000 --action=ALLOW \
    --rules=tcp:0-65535,udp:0-65535,icmp --source-ranges=10.0.0.0/8
gcloud compute firewall-rules create allow-from-corp \
    --network=hpc-vpc --direction=INGRESS --action=ALLOW --priority=1000 \
    --rules=tcp:22,icmp --source-ranges="${ssh_ip_range}"
```

### 1.4. Cloud NAT の作成

ルータと Cloud NAT を作成します。

```bash
gcloud compute routers create nat-router --network hpc-vpc
gcloud compute routers nats create nat-config --router=nat-router \
    --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges \
    --enable-logging --region "${gcp_region}"
```

## 2. クラスタの構築

### 2.1. テンプレートのダウンロード

```sh
git clone --depth 1 git@github.com:pottava/pbs-on-googlecloud.git
cd pbs-on-googlecloud/dm
```

### 2.2. クラスタ名の指定

クラスタ名（半角英数字、ハイフン、アンダースコアのみ）を指定します。

```sh
export cluster_name=
```

### 2.3. クラスタの設定変更

必要に応じて設定を置き換えてください。

- PBS 兼 NFS サーバ
  - マシン種別: `controller_machine_type`
  - ディスクサイズ: `controller_secondary_disk_size_gb`
- 計算ノード
  - マシン種別: `machine_type`
  - 起動台数: `static_node_count`

```sh
sed -e "s|cluster_name : hpc|cluster_name : ${cluster_name}|" \
    -e "s|asia-northeast1-c|${gcp_zone}|" cluster.yaml > "${cluster_name}.yaml"
vi "${cluster_name}.yaml"
```

### 2.4. Deployment Manager でのクラスタ構築

```sh
gcloud deployment-manager deployments create "${cluster_name}" --config "${cluster_name}".yaml
```

## 3. ノード・PBS の設定

本来 Google Cloud では、[OS Login](https://cloud.google.com/compute/docs/instances/managing-instance-access?hl=ja) という、より安全な SSH アクセスを実現する手段がありますが  
以下では gcloud コマンドの使えないような環境から接続する状況を想定し、一般的な SSH で設定を進めます。

### 3.1. SSH 公開鍵をプロジェクトのメタデータに登録

公開鍵の冒頭にユーザー名を挿入したファイルを、プロジェクトのメタデータとして登録します。
これにより、ここで生成する鍵で各サーバーに SSH することが許可されます。

```sh
ssh-keygen -t rsa -b 4096 -N "" -f id_rsa -q -C $(whoami)
sed "s/ssh-rsa/$(whoami):ssh-rsa/" id_rsa.pub > ssh-metadata
gcloud compute project-info add-metadata --metadata-from-file ssh-keys=ssh-metadata
```

### 3.2. ログインノードへ SSH

クラスタの構成が完了するまで進行状況をトラッキングします。‘Started Google Compute Engine Startup Scripts.’ と出力されるまで 2,3 分お待ち下さい。

```sh
login_vm=$( gcloud compute instances describe "${cluster_name}-login0" --zone "${gcp_zone}" \
    --format 'value(networkInterfaces[0].accessConfigs[0].natIP)')
ssh -i id_rsa "$(whoami)@${login_vm}" 'sudo journalctl -fu google-startup-scripts.service' \
    | grep Started
```

クラスタ内で SSH するための秘密鍵をログインノードへ転送し、SSH 接続します。

```sh
scp -i id_rsa ./id_rsa "$(whoami)@${login_vm}:~/.ssh/id_rsa"
ssh -i id_rsa "$(whoami)@${login_vm}"
```

### 3.3. PBS インストール

ヘッドノードへルートユーザで SSH 接続し、

```sh
ssh "$( curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/cluster-name )-controller"
```

Ansible を利用して、PBS をインストールします。少なくとも 15 分程度かかります。

```sh
sudo su -
cd /apps/infra/pbs-on-googlecloud/ansible
ansible-playbook --inventory inventories/hpc site.yml
```

### 3.4. ノードの登録

以下のコマンドを実行し、PBS に計算ノードを認識させます。

```sh
cluster_name=$( curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/cluster-name )
export PATH=/opt/pbs/bin:$PATH
for node in $( gcloud compute instances list \
    --filter "name~'${cluster_name}-compute.*' AND STATUS:RUNNING" --format 'value(name)' ); do
  qmgr -c "create node ${node}"
done
pbsnodes -a
```

### 3.5. キューの作成

```sh
qmgr -c "create queue batch queue_type=execution"
qmgr -c "set queue batch enabled=True"
qmgr -c "set queue batch resources_default.nodes=1"
qmgr -c "set queue batch resources_default.walltime=360000"
qmgr -c "set queue batch started=True"
qmgr -c "set server default_queue = batch"
qmgr -c "set server scheduling = True"
qmgr -c "set server node_pack = True"
qmgr -c "set server flatuid = True"
qmgr -c "set server query_other_jobs = True"
```

## 4. テスト実行

### 4.1. パスの設定

ログインノードにもどり、パスを通します。

```sh
export PATH=/opt/pbs/bin:/usr/lib64/openmpi3/bin:$PATH
```

### 4.2. 計算実行

基本的な動作を確認してみます。

```sh
cluster_name=$( curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/cluster-name )
echo "sleep 30; env | grep PBS" | qsub -l select=host="${cluster_name}-compute-0-0"
qstat -a
```

hello world プログラムを作り、実行してみます。

```sh
cat << EOF > hello.c
#include <mpi.h>
#include <stdio.h>
int main(int argc, char **argv)
{
  int rank, size;
  MPI_Init(&argc, &argv);
  MPI_Comm_rank(MPI_COMM_WORLD, &rank);
  MPI_Comm_size(MPI_COMM_WORLD, &size);
  printf("Hello World !! I am %d of %d.\n", rank, size);
  MPI_Finalize();
  return 0;
}
EOF
mpicc -o hello hello.c
mpirun -np 1 ./hello
```

```sh
cat << EOF > hello.pbs
#!/bin/bash
#PBS -N hello
#PBS -q batch
#PBS -l nodes=2:ppn=1
#PBS -l walltime=0:01:00

cd \$PBS_O_WORKDIR
export PATH=/opt/pbs/bin:/usr/lib64/openmpi3/bin:\$PATH
mpirun -np \$NCPUS -machinefile "\$PBS_NODEFILE" ./hello
EOF
qsub hello.pbs
```

## 5. リソースの削除

クラスタを停止し、

```sh
gcloud deployment-manager deployments delete "${cluster_name}"
```

基礎となるリソースも削除していきます。

```sh
gcloud compute routers nats delete nat-config --router=nat-router --region "${gcp_region}" --quiet
gcloud compute routers delete nat-router --quiet
gcloud compute firewall-rules delete allow-from-internal --quiet
gcloud compute firewall-rules delete allow-from-corp --quiet
gcloud compute networks subnets delete hpc-tokyo --region "${gcp_region}" --quiet
gcloud compute networks delete hpc-vpc --quiet
```
