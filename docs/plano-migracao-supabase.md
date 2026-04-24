# Plano Completo de Migracao para Supabase

## Objetivo

Migrar de forma incremental o que for possivel do WhatsBot para Supabase para reduzir consumo de disco/volume no Railway, mantendo estabilidade operacional e evitando regressao no fluxo principal de mensagens.

Este plano considera:
- Prioridade em aliviar limite de armazenamento no Railway.
- Ausencia de necessidade de preservar dados historicos.
- Menor risco possivel no caminho ate a Fase 4.

---

## Estado Atual (resumo tecnico)

Hoje o sistema depende fortemente de:
- SQLite local por tenant (`storages/whatsbot.db`).
- Banco mestre local (`master.db`) no modo SaaS.
- Arquivos locais em `statics/` (media, avatars, senditems) e `logs/`.
- Sessao/estado do GOWA no filesystem local do container.

Pontos mais acoplados ao SQLite:
- Fluxo de webhook e historico de mensagens.
- Memoria/contexto do agente.
- Repositorios de contato, mensagens, usage, CRM e automacoes.

---

## Estrategia Recomendada

Migracao em 4 fases (com gates de qualidade), sem big bang.

- **Fase 1 (quick win):** tirar arquivos pesados do Railway (Supabase Storage).
- **Fase 2:** mover dados administrativos/SaaS para Supabase Postgres.
- **Fase 3:** mover CRM e automacoes para Supabase Postgres.
- **Fase 4:** mover core conversacional (`contacts`, `messages`, etc.) para Supabase Postgres.

Mesmo sem dados para preservar, a Fase 4 continua sendo a mais sensivel tecnicamente por mexer no hot path do webhook.

---

## Arquitetura Alvo

### Componentes que permanecem
- Backend FastAPI (Railway).
- GOWA (subprocess local no runtime do app).
- Frontend atual (Preact/HTM sem build step).

### Componentes migrados para Supabase
- **Storage:** midias e avatars.
- **Postgres:** tabelas SaaS, CRM/automacoes e, por fim, core de mensagens/contatos.
- **Opcional futuro:** Auth e Realtime (nao necessario para reduzir volume no curto prazo).

---

## Fase 0 (preparatoria) - 2 a 4 dias

Objetivo: preparar observabilidade e seguranca para migracoes sem sustos.

### Tarefas
- Criar projeto Supabase (dev/staging/prod).
- Configurar variaveis no Railway:
  - `SUPABASE_URL`
  - `SUPABASE_ANON_KEY` (somente frontend, se necessario)
  - `SUPABASE_SERVICE_ROLE_KEY` (somente backend)
  - `SUPABASE_DB_URL` (se usar conexao Postgres direta)
- Definir padrao de segredo:
  - Nunca expor `service_role` no frontend.
- Criar checklist de rollback por fase.
- Criar dashboard basico de saude:
  - latencia media do webhook
  - taxa de erro por endpoint
  - tamanho de volume em disco
  - crescimento diario de midia

### Gate de saida
- Ambientes Supabase acessiveis.
- Segredos carregados no Railway.
- Telemetria minima funcionando.

---

## Fase 1 - Storage (baixo risco, alto retorno)

Objetivo: reduzir crescimento de disco local no Railway movendo arquivos para Supabase Storage.

Estimativa: 4 a 8 dias.

### Escopo
- Upload de arquivos recebidos/enviados (media e avatars) para bucket no Supabase.
- Persistir URL/chave do objeto em vez de caminho local de arquivo quando possivel.
- Manter fallback local temporario por feature flag.

### Mudancas tecnicas
- Introduzir modulo de storage provider (ex.: `local` e `supabase`).
- Ajustar pontos de escrita/leitura em rotas de webhook/contacts.
- Atualizar frontend para consumir URL remota quando presente.

### Feature flags sugeridas
- `STORAGE_BACKEND=local|supabase`
- `STORAGE_WRITE_THROUGH=true|false` (opcional para transicao)

### Testes obrigatorios
- Envio e recebimento de imagem/audio.
- Reprocesso de mensagem com anexos.
- Renderizacao no frontend com URL assinada/publica.
- Queda do provider remoto (deve degradar com erro controlado).

### Gate de saida
- 100% de novos anexos salvos no Supabase Storage.
- Queda perceptivel no uso de volume local.
- Sem regressao no fluxo de atendimento.

---

## Fase 2 - Dados SaaS/Administrativos (risco medio)

Objetivo: mover dados de controle do SaaS para Supabase Postgres.

Estimativa: 1 a 2 semanas.

### Escopo de tabelas
- `tenants`
- `superadmins`
- `global_config`
- `tenant_policies`
- `tenant_company_profile`
- `tenant_billing_invoices`

### Estrategia de migracao
- Criar repositorios Postgres equivalentes aos `master_*_repo`.
- Isolar acesso por interface de repositorio.
- Fazer cutover por feature flag:
  - `MASTER_DB_BACKEND=sqlite|supabase`

### Testes obrigatorios
- Setup inicial de superadmin.
- Login admin.
- CRUD de tenants.
- Politicas por tenant.
- Leitura de faturamento/invoices.

### Gate de saida
- Painel admin operando 100% no Supabase.
- `master.db` deixa de ser fonte principal.

---

## Fase 3 - CRM e Automacoes (risco medio-alto)

Objetivo: migrar dominio de negocio de CRM e automacoes para Supabase.

Estimativa: 2 a 4 semanas.

### Escopo de tabelas
- `crm_deals`
- `crm_tasks`
- `automation_rules`
- `automation_runs`

### Pontos de atencao
- Fluxos de automacao acionados por mudanca de estagio.
- Consistencia entre `contact_id`, deal e regras.
- Execucao de webhooks externos com retries.

### Estrategia de seguranca
- Ativar RLS nas tabelas expostas.
- Politicas por tenant (`tenant_id` obrigatorio no modelo).
- Indexacao para consultas de lista/filtro/status.

### Feature flag
- `CRM_AUTOMATION_BACKEND=sqlite|supabase`

### Testes obrigatorios
- CRUD completo de regras.
- Simulacao de automacao.
- Mudanca de estagio no CRM disparando run.
- Historico de execucoes sem inconsistencias.

### Gate de saida
- CRM e automacoes estaveis por ao menos 7 dias.
- Taxa de erro e latencia dentro do baseline aceito.

---

## Fase 4 - Core Conversacional (risco alto)

Objetivo: migrar o nucleo de dados do bot para Supabase Postgres.

Estimativa: 4 a 8+ semanas.

### Escopo de tabelas core
- `contacts`
- `messages`
- `observations`
- `tags`
- `contact_tags`
- `unread_msg_ids`
- `usage` (se ainda nao migrado)
- `executions`
- `execution_steps`

### Por que continua dificil mesmo sem dados legados
- Alto acoplamento do fluxo webhook + agente + memoria.
- Altissimo volume de escrita/leitura no caminho critico.
- Mudanca de latencia (SQLite local -> Postgres remoto).
- Necessidade de revisar concorrencia/transacoes.

### Estrategia recomendada (sem dados para preservar)
1. Criar schema novo no Supabase (sem migracao historica).
2. Ligar ambiente de staging limpo com feature flags.
3. Rodar smoke + carga controlada.
4. Fazer canario parcial (ex.: 10% tenants).
5. Expandir para 50% e depois 100%.
6. Desligar caminho SQLite apenas apos estabilidade comprovada.

### Feature flags
- `CORE_DB_BACKEND=sqlite|supabase`
- `CORE_CANARY_TENANTS=...` (lista de slugs)

### Gate de saida
- Latencia de webhook dentro de limite definido.
- Sem perda de mensagens.
- Sem regressao de contexto do agente.
- Incidentes criticos = 0 por periodo acordado.

---

## Ordem sugerida de execucao (pratica)

1. Fase 0 (preparo).
2. Fase 1 (Storage) - maior ganho por menor risco.
3. Fase 2 (master/admin).
4. Fase 3 (CRM/automacoes).
5. Fase 4 (core) apenas quando produto estiver estavel.

---

## Plano de Rollback por Fase

Todos os rollbacks devem ser acionados por feature flag, sem deploy emergencial quando possivel.

### Rollback Fase 1
- Voltar `STORAGE_BACKEND=local`.
- Manter links antigos para leitura ate limpeza planejada.

### Rollback Fase 2
- Voltar `MASTER_DB_BACKEND=sqlite`.
- Preservar escrita congelada no Supabase para auditoria.

### Rollback Fase 3
- Voltar `CRM_AUTOMATION_BACKEND=sqlite`.
- Reprocessar eventos de automacao pendentes se necessario.

### Rollback Fase 4
- Voltar `CORE_DB_BACKEND=sqlite`.
- Pausar ingestao por minutos para evitar divergencia em ida/volta.
- Reabrir trafego apos validacao de saude.

---

## Criterios de Sucesso

### Tecnicos
- Reducao consistente de uso de volume no Railway.
- Sem perda de mensagens e sem duplicidade em runs criticos.
- Latencia de endpoints principais estavel.
- Cobertura de testes para fluxos criticos de webhook e CRM.

### Operacionais
- Processo de deploy previsivel com flags.
- Time capaz de fazer rollback em minutos.
- Documentacao de operacao atualizada em `docs/`.

---

## Esforco Consolidado

- **Fase 0:** baixa
- **Fase 1:** baixa/media
- **Fase 2:** media
- **Fase 3:** media/alta
- **Fase 4:** alta (continua alta mesmo sem dados legados)

Prazo total realista (sem paralelizacao agressiva): **8 a 16 semanas**.

Com equipe dedicada e paralelizacao forte, pode cair para **6 a 10 semanas**.

---

## Recomendacao Executiva

Se o objetivo imediato e nao estourar limite do Railway:
- Execute **Fase 1 + Fase 2** primeiro.
- So entre na **Fase 4** quando houver necessidade real de escala/robustez no core.

Isso captura grande parte do beneficio com risco controlado.
