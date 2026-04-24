# Automações CRM: Segurança de Webhook e Operação

Guia prático para operar automações CRM com segurança em produção.

## 1) Segurança de Webhook (obrigatório)

As automações de ação `webhook` possuem proteção nativa:

- HTTPS obrigatório por padrão
- bloqueio de hosts locais/privados (`localhost`, `127.0.0.1`, ranges privados)
- whitelist de domínios permitidos (opcional, porém recomendado)
- retries com backoff para falhas transitórias

### Configurações por tenant

Essas chaves ficam em `config` (tenant DB) e podem ser salvas via `PUT /api/config`:

- `automation_webhook_allowed_domains` (lista)
  - Exemplo: `["api.minhaempresa.com", "hooks.zapier.com"]`
  - Quando preenchida, qualquer domínio fora da lista é bloqueado.
- `automation_webhook_allow_http` (bool, default `false`)
  - Mantenha `false` em produção.
- `automation_webhook_block_private_hosts` (bool, default `true`)
  - Mantenha `true` em produção.
- `automation_webhook_max_retries` (int, default `1`, máx `5`)
- `automation_webhook_retry_backoff_seconds` (float, default `0.8`, entre `0.1` e `10.0`)

### Exemplo de atualização (cURL)

```bash
curl -X PUT "http://127.0.0.1:8080/api/config" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer SEU_TOKEN" \
  -d '{
    "automation_webhook_allowed_domains": ["api.minhaempresa.com", "hooks.zapier.com"],
    "automation_webhook_allow_http": false,
    "automation_webhook_block_private_hosts": true,
    "automation_webhook_max_retries": 2,
    "automation_webhook_retry_backoff_seconds": 1.0
  }'
```

## 2) Checklist de publicação de regra

Antes de ativar uma regra:

1. Criar regra em modo desativado.
2. Rodar **Testar regra** no painel de automações.
3. Validar placeholders renderizados (`title_template`, `body_template`).
4. Confirmar que webhook aponta para domínio permitido.
5. Ativar regra e monitorar `Últimas execuções` por 15-30 min.

## 3) Playbook de incidentes

### 3.1 Regra em loop

Sinais:
- muitos runs em sequência para o mesmo `deal_id`
- alternância repetida de estágio

Ações:
1. Desativar regra no painel.
2. Verificar histórico (`status: skipped`) para dedupe/no-op.
3. Revisar condições e ação de `move_stage`.

### 3.2 Webhook falhando

Sinais:
- runs com `status=error`
- `status_code >= 500` ou erro de URL/segurança

Ações:
1. Confirmar URL e método.
2. Verificar whitelist (`automation_webhook_allowed_domains`).
3. Validar TLS/HTTPS no endpoint.
4. Ajustar retries/backoff se necessário.

### 3.3 Regra não dispara

Sinais:
- simulação retorna `will_run=false`

Ações:
1. Conferir `from_stage` e `to_stage`.
2. Conferir condições (`owner`, `tag`, `min_value`).
3. Confirmar que a regra está `enabled=true`.

## 4) Boas práticas

- Use whitelist sempre em produção.
- Evite regras de mover estágio em ciclo (A->B e B->A).
- Prefira payloads webhook pequenos e determinísticos.
- Use template com campos estáveis (`deal.id`, `contact.phone`).
- Faça rollout gradual (1 tenant -> 3 tenants -> geral).

## 5) Auditoria mínima recomendada

Semanalmente:
- top 10 regras com mais erro
- top 10 regras com mais `skipped` (pode indicar configuração ruim)
- webhooks com maior latência/timeout

