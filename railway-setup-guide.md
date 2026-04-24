# 🚂 Configuração do WhatsBot SaaS no Railway

## Situação atual
- ✅ Sistema rodando no Railway com domínio padrão (`.up.railway.app`)
- ✅ Código atualizado com suporte multi-tenant (modo `single` ativo por padrão)
- ⏳ Dados **não persistem** entre deploys (SQLite e sessão do WhatsApp somem)

---

## Etapa 1 — Resolver a Persistência (URGENTE — fazer agora)

> [!CAUTION]
> Sem isso, **toda vez que você fizer deploy** o banco de dados e a sessão do WhatsApp são perdidos. Isso já acontece hoje com o sistema atual.

### 1.1 Criar um Volume no Railway

1. Abra seu projeto no [Railway Dashboard](https://railway.app/dashboard)
2. Clique no serviço do WhatsBot
3. Vá na aba **"Volumes"**
4. Clique em **"Add Volume"** (ou "New Volume")
5. Preencha:
   - **Mount Path:** `/data`
   - **Size:** mínimo 1GB (aumentar conforme crescer)
6. Clique em **"Create"**

### 1.2 Adicionar variável de ambiente

1. Ainda no serviço, vá na aba **"Variables"**
2. Clique em **"Add Variable"**
3. Adicione:

```
WHATSBOT_DATA_DIR = /data
```

4. Clique em **"Deploy"** para aplicar

### 1.3 Verificar que funcionou

Após o deploy, abra o painel do WhatsBot e configure tudo novamente (API key, prompt, escaneie o QR). Faça um novo deploy e verifique que os dados continuam. ✅

---

## Etapa 2 — Atualizar o Código (quando terminar o desenvolvimento)

Quando o frontend do Superadmin estiver pronto, faça:

```bash
git add .
git commit -m "feat: multi-tenant SaaS infrastructure"
git push
```

O Railway vai detectar o push e fazer deploy automaticamente.

> [!NOTE]
> Enquanto `WHATSBOT_MODE` não estiver como `saas`, o sistema continua funcionando **exatamente igual ao de hoje** — modo single-tenant. Nenhuma empresa vai notar diferença.

---

## Etapa 3 — Ativar o Modo SaaS

### 3.1 Adicionar as variáveis de ambiente

Na aba **"Variables"** do serviço, adicione:

| Variável | Valor |
|---|---|
| `WHATSBOT_MODE` | `saas` |
| `WHATSBOT_DOMAIN` | *(ver opções abaixo)* |

### 3.2 ⚠️ O problema do domínio Railway

O domínio padrão do Railway (`seu-projeto.up.railway.app`) **não suporta wildcard**.

Para o SaaS funcionar com subdomínios (`empresa1.bot.seudominio.com.br`, `empresa2.bot.seudominio.com.br`), você precisa de **uma das duas opções**:

---

## Etapa 4 — Domínio (escolha uma opção)

### ✅ Usar subdomínio do seu domínio atual (sem afetar a Vercel)

Como você já tem um domínio apontando para a Vercel, a solução é criar um **subdomínio dedicado** para o WhatsBot. O domínio principal e a Vercel não são afetados.

**Estrutura resultante:**
```
seudominio.com.br              → Vercel (seu site, não muda nada)
admin.bot.seudominio.com.br    → Railway (painel superadmin)
empresa1.bot.seudominio.com.br → Railway (painel empresa 1)
empresa2.bot.seudominio.com.br → Railway (painel empresa 2)
```

#### Passo 1 — No Registro.br

1. Acesse [registro.br](https://registro.br) → seu domínio → **Editar zona DNS**
2. Adicione **dois registros CNAME**:

| Tipo | Nome | Valor (destino) |
|---|---|---|
| `CNAME` | `bot` | `seu-projeto.up.railway.app` |
| `CNAME` | `*.bot` | `seu-projeto.up.railway.app` |

> O primeiro (`bot`) resolve o domínio base. O segundo (`*.bot`) é o wildcard que captura todos os subdomínios automaticamente.

#### Passo 2 — No Railway

1. Vá em **"Settings"** → **"Networking"** → **"Custom Domain"**
2. Clique em **"Add Domain"**
3. Digite: `*.bot.seudominio.com.br`
4. Railway vai gerar o certificado SSL automaticamente _(pode levar até 10 minutos)_

#### Passo 3 — Variável de ambiente no Railway

Na aba **"Variables"**, adicione:
```
WHATSBOT_DOMAIN = bot.seudominio.com.br
```

> [!TIP]
> Após a propagação do DNS (geralmente 5-30 minutos), teste acessando `bot.seudominio.com.br` — deve abrir o WhatsBot. Se funcionar, o wildcard também vai funcionar.

---

## Etapa 5 — Primeiro Acesso ao Painel Superadmin

Após ativar o modo SaaS com domínio próprio:

1. Acesse `admin.bot.seudominio.com.br`
2. Você verá a tela de **Setup Inicial**
3. Crie seu usuário e senha de Superadmin
4. Faça login
5. Crie a primeira empresa:
   - **Nome:** Nome da empresa
   - **Slug:** `empresa1` (vai virar `empresa1.bot.seudominio.com.br`)
6. Acesse `empresa1.bot.seudominio.com.br`
7. Conecte o WhatsApp via QR Code
8. Configure o prompt e a API key da OpenRouter
9. ✅ Pronto — empresa funcionando

---

## Resumo de todas as variáveis no Railway

| Variável | Valor | Etapa |
|---|---|---|
| `WHATSBOT_DATA_DIR` | `/data` | Etapa 1 — **fazer agora** |
| `WHATSBOT_MODE` | `saas` | Etapa 3 — quando ativar SaaS |
| `WHATSBOT_DOMAIN` | `bot.seudominio.com.br` | Etapa 4 — com domínio próprio |
| `WHATSBOT_DOCKER` | `1` | Já existe |
| `WHATSBOT_WEB_PORT` | `8080` | Já existe (ou padrão) |

---

## O que NÃO precisa mudar no Railway

- ✅ Build command — continua igual (Railway detecta o Dockerfile)
- ✅ Start command — continua `python main.py`  
- ✅ Porta exposta — continua `8080`
- ✅ Health check — já está no Dockerfile
- ✅ Plano — o Railway Pro ou o plano atual já é suficiente para começar
