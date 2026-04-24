# Guia de Operação: Migração para Supabase

## Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SUPABASE_URL` | Fase 1+ | URL do projeto Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | Fase 1+ | Chave service_role (somente backend) |
| `SUPABASE_DB_URL` | Fase 2+ | DSN Postgres direto (`postgresql://...`) |
| `STORAGE_BACKEND` | Fase 1 | `local` (padrão) ou `supabase` |
| `STORAGE_WRITE_THROUGH` | Fase 1 | `false` (padrão) ou `true` |
| `MASTER_DB_BACKEND` | Fase 2 | `sqlite` (padrão) ou `supabase` |

> **Nunca exponha `SUPABASE_SERVICE_ROLE_KEY` nem `SUPABASE_DB_URL` no frontend.**

---

## Fase 0 — Preparação

1. Criar projeto no [Supabase](https://supabase.com) (dev + prod).
2. Copiar **Project URL** e **service_role key** (Settings → API).
3. Copiar **Connection String / Direct connection** (Settings → Database) para `SUPABASE_DB_URL`.
4. Adicionar as variáveis no Railway (Settings → Variables).
5. Verificar conectividade:
   ```bash
   SUPABASE_URL=https://xxx.supabase.co \
   SUPABASE_SERVICE_ROLE_KEY=eyJ... \
   python -c "from db.supabase_client import get_client; print(get_client())"
   ```

### Gate de saída
- [ ] Variáveis carregadas no Railway sem erros de deploy
- [ ] `get_client()` retorna sem exceção

---

## Fase 1 — Storage (arquivos de mídia)

### Ativação

```bash
# 1. Criar buckets (rodar uma única vez)
SUPABASE_URL=https://xxx.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=eyJ... \
python scripts/setup_supabase_buckets.py

# 2. (Opcional) Ativar write-through para transição gradual
# STORAGE_WRITE_THROUGH=true  → salva em local E Supabase

# 3. Quando satisfeito, ativar somente Supabase
STORAGE_BACKEND=supabase
```

### Rollback

```bash
STORAGE_BACKEND=local
```

Links antigos (URLs do Supabase já gravados no banco) continuam funcionando.
Novos uploads voltam para o disco local.

### Testes obrigatórios antes de marcar Gate como OK

- [ ] Envio de imagem pelo operador (POST /api/contacts/{phone}/send-image)
- [ ] Envio de áudio pelo operador (POST /api/contacts/{phone}/send-audio)
- [ ] Recebimento de mídia via webhook (imagem + áudio)
- [ ] Avatar de contato carregado no frontend
- [ ] Renderização de histórico com mídia antiga (path local) ainda funciona
- [ ] Simular queda do Supabase → app deve degradar com erro 500 controlado, não crash

### Gate de saída

- [ ] 100% dos novos uploads em Supabase Storage
- [ ] Uso de volume no Railway em queda perceptível após 24h
- [ ] Zero regressão no fluxo de atendimento

---

## Fase 2 — Master DB (dados SaaS/Admin)

### Ativação

```bash
# 1. SUPABASE_DB_URL deve estar setado (ver Fase 0)
# 2. Ao subir o app com a flag, as tabelas são criadas automaticamente (DDL idempotente)
MASTER_DB_BACKEND=supabase
```

O schema Postgres equivalente ao `master_schema.sql` é aplicado automaticamente
via `db/master_pg_connection.py` na primeira subida.

### Rollback

```bash
MASTER_DB_BACKEND=sqlite
```

O `master.db` local nunca é deletado automaticamente — basta reverter a flag.
Os dados escritos no Supabase ficam preservados para auditoria.

### Testes obrigatórios antes de marcar Gate como OK

- [ ] Setup inicial de superadmin via `/api/admin/setup`
- [ ] Login de superadmin
- [ ] CRUD de tenants (criar, editar, suspender, ativar, deletar)
- [ ] Policies por tenant (get + set)
- [ ] Criação/edição de perfil da empresa (`tenant_company_profile`)
- [ ] Criação, edição e exclusão de faturas
- [ ] `ensure_next_three_open_invoices` gerando faturas corretamente
- [ ] Resumo financeiro retornando valores corretos

### Gate de saída

- [ ] Painel admin 100% operando via Supabase Postgres
- [ ] `master.db` local não é mais a fonte de verdade
- [ ] Nenhum 500 por problema de conexão em 24h de operação

---

## Fase 3 — CRM e Automações

### Ativação

```bash
# As tabelas serão criadas na primeira subida se MASTER_DB_BACKEND já for supabase
CRM_AUTOMATION_BACKEND=supabase
```

Os dados locais não migram automaticamente. As tabelas em Postgres exigirão
a recriação das regras de automação (pode ser via painel).

### Rollback

```bash
CRM_AUTOMATION_BACKEND=sqlite
```
Os dados no Postgres permanecem salvos, e o sistema volta a usar as tabelas antigas de cada tenant localmente (`whatsbot.db`).

---

## Fase 4 — Core Conversacional

> **Ainda não implementada no código.**  
> Ficará sob a flag `CORE_DB_BACKEND=sqlite|supabase`.

---

## Diagnóstico Rápido

```bash
# Verificar backend de storage ativo
python -c "from db.storage_provider import _backend; print('Storage:', _backend())"

# Verificar backend master DB ativo
python -c "from db.master_pg_connection import is_supabase_backend; print('Master:', 'supabase' if is_supabase_backend() else 'sqlite')"

# Testar upload manual
python -c "
from pathlib import Path
from db.storage_provider import init_provider, get_provider
p = init_provider(Path('.'))
url = p.upload('media', 'test.txt', b'hello', 'text/plain')
print('URL:', url)
"
```
