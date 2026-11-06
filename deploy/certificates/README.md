# 開発用 TLS 証明書の生成手順

`DEV-001` では、Nginx edge で TLS 終端を行うための自己署名開発用証明書をローカルで生成します。
秘密鍵および生成された証明書ファイルは Git 管理外の `var/certificates/` に出力されます。

## 生成コマンド

リポジトリルートで以下を実行します（`./scripts/up.sh` や `make up` 実行時にも未生成であれば自動実行されます）。

```bash
./scripts/generate-certs.sh
```

再生成する場合は `--force` を指定します。

```bash
./scripts/generate-certs.sh --force
```

## 出力先（Git 管理外）

- `var/certificates/edge.key` : 秘密鍵（RSA 2048bit）
- `var/certificates/edge.crt` : 自己署名サーバー証明書（SAN: `app-a.local.test`, `app-b.local.test`, `*.local.test`, `localhost`, `127.0.0.1`）

## クライアント検証方法

OS の証明書ストアへの登録は必須ではありません。`curl` 等で検証する際は `--cacert var/certificates/edge.crt` および `--resolve` を使用できます。

```bash
curl --cacert var/certificates/edge.crt \
  --resolve "app-a.local.test:8443:127.0.0.1" \
  https://app-a.local.test:8443/
```
