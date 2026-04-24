import { h } from 'preact';
import { useCallback, useEffect, useState } from 'preact/hooks';
import htm from 'htm';
import {
  createAutomationRule,
  deleteAutomationRule,
  getAutomationRules,
  getAutomationRuns,
  simulateAutomationRule,
  updateAutomationRule,
} from '../services/api.js';

const html = htm.bind(h);

const STAGES = ['novo', 'em_atendimento', 'proposta', 'fechado_ganho', 'perdido'];
const STAGE_LABELS = {
  novo: 'Novo',
  em_atendimento: 'Em atendimento',
  proposta: 'Proposta',
  fechado_ganho: 'Fechado (ganho)',
  perdido: 'Perdido',
};

function formatTs(ts) {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleString('pt-BR');
}

function statusLabel(status) {
  if (status === 'ok') return 'OK';
  if (status === 'skipped') return 'Ignorada';
  return 'Erro';
}

export function Automations() {
  const [rules, setRules] = useState([]);
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const [name, setName] = useState('');
  const [fromStage, setFromStage] = useState('');
  const [toStage, setToStage] = useState('');
  const [actionType, setActionType] = useState('create_task');
  const [taskTitleTemplate, setTaskTitleTemplate] = useState('Follow-up: {{deal.title}}');
  const [taskNotesTemplate, setTaskNotesTemplate] = useState('Contato: {{contact.phone}}');
  const [moveToStage, setMoveToStage] = useState('em_atendimento');
  const [webhookUrl, setWebhookUrl] = useState('');
  const [webhookMethod, setWebhookMethod] = useState('POST');
  const [webhookHeadersJson, setWebhookHeadersJson] = useState('{}');
  const [webhookBodyTemplate, setWebhookBodyTemplate] = useState('{"deal_id":"{{deal.id}}","to_stage":"{{deal.to_stage}}","phone":"{{contact.phone}}"}');
  const [conditionOwner, setConditionOwner] = useState('');
  const [conditionTag, setConditionTag] = useState('');
  const [conditionMinValue, setConditionMinValue] = useState('');
  const [lastSimulation, setLastSimulation] = useState(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError('');
    const [rulesRes, runsRes] = await Promise.all([
      getAutomationRules(),
      getAutomationRuns({ limit: 50 }),
    ]);
    if (!rulesRes.ok) {
      setError(rulesRes.error || 'Falha ao carregar regras.');
    } else {
      setRules(rulesRes.data?.items || []);
    }
    if (runsRes.ok) {
      setRuns(runsRes.data?.items || []);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  async function createRule(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    let headers = {};
    if (actionType === 'webhook') {
      try {
        headers = webhookHeadersJson.trim() ? JSON.parse(webhookHeadersJson) : {};
      } catch (_) {
        setSaving(false);
        setError('Headers do webhook devem ser um JSON válido.');
        return;
      }
    }
    const action_payload = actionType === 'create_task'
      ? { title_template: taskTitleTemplate.trim(), notes_template: taskNotesTemplate.trim() }
      : actionType === 'move_stage'
        ? { to_stage: moveToStage }
        : { url: webhookUrl.trim(), method: webhookMethod, headers, body_template: webhookBodyTemplate };
    const res = await createAutomationRule({
      name: name.trim(),
      trigger_type: 'deal_stage_changed',
      from_stage: fromStage,
      to_stage: toStage,
      conditions: {
        owner: conditionOwner.trim(),
        contact_tag: conditionTag.trim(),
        min_value: conditionMinValue.trim(),
      },
      action_type: actionType,
      action_payload,
      enabled: true,
    });
    setSaving(false);
    if (!res.ok) {
      setError(res.error || 'Erro ao criar regra.');
      return;
    }
    setName('');
    setFromStage('');
    setToStage('');
    setTaskTitleTemplate('Follow-up: {{deal.title}}');
    setTaskNotesTemplate('Contato: {{contact.phone}}');
    setMoveToStage('em_atendimento');
    setWebhookUrl('');
    setWebhookMethod('POST');
    setWebhookHeadersJson('{}');
    setWebhookBodyTemplate('{"deal_id":"{{deal.id}}","to_stage":"{{deal.to_stage}}","phone":"{{contact.phone}}"}');
    setConditionOwner('');
    setConditionTag('');
    setConditionMinValue('');
    await loadData();
  }

  async function toggleRule(rule) {
    const res = await updateAutomationRule(rule.id, { enabled: !rule.enabled });
    if (!res.ok) return setError(res.error || 'Erro ao atualizar regra.');
    await loadData();
  }

  async function removeRule(rule) {
    if (!window.confirm(`Excluir a regra "${rule.name}"?`)) return;
    const res = await deleteAutomationRule(rule.id);
    if (!res.ok) return setError(res.error || 'Erro ao excluir regra.');
    await loadData();
  }

  async function testRule(rule) {
    const rawDealId = window.prompt(`Informe o ID da oportunidade para testar a regra "${rule.name}":`);
    if (!rawDealId) return;
    const dealId = parseInt(rawDealId, 10);
    if (!Number.isFinite(dealId) || dealId <= 0) {
      setError('ID da oportunidade inválido para simulação.');
      return;
    }
    const fromStageInput = window.prompt(
      'Estágio de origem para simulação (opcional). Ex.: novo',
      rule.from_stage || '',
    ) || '';
    setError('');
    const res = await simulateAutomationRule(rule.id, {
      deal_id: dealId,
      from_stage: fromStageInput.trim(),
    });
    if (!res.ok) {
      setError(res.error || 'Erro ao simular regra.');
      return;
    }
    setLastSimulation({
      ruleName: rule.name,
      data: res.data?.result || null,
    });
  }

  if (loading) return html`<div class="text-sm text-wa-secondary">Carregando automações...</div>`;

  return html`
    <div class="space-y-4">
      <form onSubmit=${createRule} class="bg-white rounded-xl p-4 border border-wa-border space-y-3">
        <h3 class="text-sm font-semibold">Nova automação de CRM</h3>
        <div class="grid grid-cols-1 md:grid-cols-5 gap-2">
          <input value=${name} onInput=${(e) => setName(e.target.value)} class="border rounded px-3 py-2 md:col-span-2" placeholder="Nome da regra" required />
          <select value=${fromStage} onChange=${(e) => setFromStage(e.target.value)} class="border rounded px-3 py-2">
            <option value="">Qualquer estágio origem</option>
            ${STAGES.map((s) => html`<option value=${s}>${STAGE_LABELS[s]}</option>`)}
          </select>
          <select value=${toStage} onChange=${(e) => setToStage(e.target.value)} class="border rounded px-3 py-2">
            <option value="">Qualquer estágio destino</option>
            ${STAGES.map((s) => html`<option value=${s}>${STAGE_LABELS[s]}</option>`)}
          </select>
          <select value=${actionType} onChange=${(e) => setActionType(e.target.value)} class="border rounded px-3 py-2">
            <option value="create_task">Ação: Criar tarefa</option>
            <option value="move_stage">Ação: Mover estágio</option>
            <option value="webhook">Ação: Webhook externo</option>
          </select>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-2">
          <input value=${conditionOwner} onInput=${(e) => setConditionOwner(e.target.value)} class="border rounded px-3 py-2" placeholder="Condição: responsável contém..." />
          <input value=${conditionTag} onInput=${(e) => setConditionTag(e.target.value)} class="border rounded px-3 py-2" placeholder="Condição: tag do contato" />
          <input value=${conditionMinValue} onInput=${(e) => setConditionMinValue(e.target.value)} type="number" step="0.01" class="border rounded px-3 py-2" placeholder="Condição: valor mínimo" />
        </div>
        ${actionType === 'create_task' ? html`
          <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
            <input value=${taskTitleTemplate} onInput=${(e) => setTaskTitleTemplate(e.target.value)} class="border rounded px-3 py-2" placeholder="Template título (ex.: Follow-up {{deal.title}})" />
            <input value=${taskNotesTemplate} onInput=${(e) => setTaskNotesTemplate(e.target.value)} class="border rounded px-3 py-2" placeholder="Template notas (ex.: Telefone {{contact.phone}})" />
          </div>
        ` : actionType === 'move_stage' ? html`
          <div class="grid grid-cols-1 md:grid-cols-3 gap-2">
            <select value=${moveToStage} onChange=${(e) => setMoveToStage(e.target.value)} class="border rounded px-3 py-2">
              ${STAGES.map((s) => html`<option value=${s}>Mover para: ${STAGE_LABELS[s]}</option>`)}
            </select>
          </div>
        ` : html`
          <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
            <input value=${webhookUrl} onInput=${(e) => setWebhookUrl(e.target.value)} class="border rounded px-3 py-2" placeholder="URL do webhook (https://...)" />
            <select value=${webhookMethod} onChange=${(e) => setWebhookMethod(e.target.value)} class="border rounded px-3 py-2">
              <option value="POST">POST</option>
              <option value="PUT">PUT</option>
              <option value="PATCH">PATCH</option>
              <option value="DELETE">DELETE</option>
              <option value="GET">GET</option>
            </select>
            <textarea value=${webhookHeadersJson} onInput=${(e) => setWebhookHeadersJson(e.target.value)} class="border rounded px-3 py-2 md:col-span-2" rows="2" placeholder='Headers JSON (ex.: {"Authorization":"Bearer ..."} )'></textarea>
            <textarea value=${webhookBodyTemplate} onInput=${(e) => setWebhookBodyTemplate(e.target.value)} class="border rounded px-3 py-2 md:col-span-2" rows="3" placeholder='Body template JSON (ex.: {"deal_id":"{{deal.id}}"} )'></textarea>
          </div>
        `}
        <div class="text-xs text-wa-secondary">
          Variáveis disponíveis: {{deal.id}}, {{deal.title}}, {{deal.from_stage}}, {{deal.to_stage}}, {{deal.owner}}, {{deal.potential_value}}, {{contact.phone}}, {{contact.name}}, {{contact.tags_csv}}, {{now_iso}}.
        </div>
        <button disabled=${saving} class="bg-wa-teal text-white rounded px-3 py-2 text-sm disabled:opacity-60">
          ${saving ? 'Salvando...' : 'Salvar regra'}
        </button>
      </form>

      ${error ? html`<div class="text-sm text-red-600">${error}</div>` : null}

      <div class="bg-white rounded-xl p-4 border border-wa-border space-y-3">
        <h3 class="text-sm font-semibold">Regras ativas</h3>
        ${rules.length === 0 ? html`
          <div class="text-sm text-wa-secondary">Nenhuma regra cadastrada.</div>
        ` : html`
          <div class="space-y-2">
            ${rules.map((rule) => html`
              <div class="border rounded p-3 text-sm flex flex-col md:flex-row md:items-center md:justify-between gap-2">
                <div>
                  <div class="font-medium">${rule.name}</div>
                  <div class="text-wa-secondary text-xs">
                    Trigger: mudança de estágio · origem: ${rule.from_stage ? (STAGE_LABELS[rule.from_stage] || rule.from_stage) : 'qualquer'}
                    · destino: ${rule.to_stage ? (STAGE_LABELS[rule.to_stage] || rule.to_stage) : 'qualquer'}
                  </div>
                  <div class="text-wa-secondary text-xs">
                    Condições: responsável=${rule.conditions?.owner || 'qualquer'} · tag=${rule.conditions?.contact_tag || 'qualquer'} · valor>=${rule.conditions?.min_value ?? 'qualquer'}
                  </div>
                  <div class="text-wa-secondary text-xs">
                    Ação: ${rule.action_type === 'create_task'
                      ? `criar tarefa (${rule.action_payload?.title_template || rule.action_payload?.title || 'sem título'})`
                      : rule.action_type === 'move_stage'
                        ? `mover para ${STAGE_LABELS[rule.action_payload?.to_stage] || rule.action_payload?.to_stage || '-'}`
                        : `webhook ${rule.action_payload?.method || 'POST'} ${rule.action_payload?.url || '-'}`
                    }
                  </div>
                </div>
                <div class="flex gap-2">
                  <button onClick=${() => testRule(rule)} class="border rounded px-2 py-1 text-xs">
                    Testar regra
                  </button>
                  <button onClick=${() => toggleRule(rule)} class="border rounded px-2 py-1 text-xs">
                    ${rule.enabled ? 'Desativar' : 'Ativar'}
                  </button>
                  <button onClick=${() => removeRule(rule)} class="border border-red-200 text-red-700 rounded px-2 py-1 text-xs">
                    Excluir
                  </button>
                </div>
              </div>
            `)}
          </div>
        `}
      </div>

      ${lastSimulation ? html`
        <div class="bg-white rounded-xl p-4 border border-wa-border space-y-3">
          <h3 class="text-sm font-semibold">Última simulação: ${lastSimulation.ruleName}</h3>
          <div class="text-xs text-wa-secondary">
            Vai executar: ${lastSimulation.data?.will_run ? 'SIM' : 'NÃO'} · enabled=${String(lastSimulation.data?.enabled)}
            · stage(from/to)=${lastSimulation.data?.stage_match?.from_ok ? 'ok' : 'falhou'}/${lastSimulation.data?.stage_match?.to_ok ? 'ok' : 'falhou'}
            · condições=${lastSimulation.data?.conditions_match ? 'ok' : 'falharam'}
          </div>
          <pre class="text-xs bg-gray-50 border border-gray-200 rounded p-2 overflow-x-auto">${JSON.stringify(lastSimulation.data, null, 2)}</pre>
        </div>
      ` : null}

      <div class="bg-white rounded-xl p-4 border border-wa-border space-y-3">
        <h3 class="text-sm font-semibold">Últimas execuções</h3>
        ${runs.length === 0 ? html`
          <div class="text-sm text-wa-secondary">Nenhuma execução registrada.</div>
        ` : html`
          <div class="overflow-x-auto">
            <table class="w-full text-xs">
              <thead>
                <tr class="border-b">
                  <th class="text-left py-2 pr-2">Quando</th>
                  <th class="text-left py-2 pr-2">Regra</th>
                  <th class="text-left py-2 pr-2">Status</th>
                  <th class="text-left py-2 pr-2">Contexto</th>
                  <th class="text-left py-2">Resultado/Erro</th>
                </tr>
              </thead>
              <tbody>
                ${runs.map((run) => html`
                  <tr class="border-b">
                    <td class="py-2 pr-2">${formatTs(run.ts)}</td>
                    <td class="py-2 pr-2">#${run.rule_id || '-'}</td>
                    <td class="py-2 pr-2">${statusLabel(run.status)}</td>
                    <td class="py-2 pr-2">
                      ${run.context?.from_stage || '-'} -> ${run.context?.to_stage || '-'} (${run.context?.contact_phone || '-'})
                    </td>
                    <td class="py-2">
                      ${run.status === 'error' ? (run.error || '-') : JSON.stringify(run.result || {})}
                    </td>
                  </tr>
                `)}
              </tbody>
            </table>
          </div>
        `}
      </div>
    </div>
  `;
}
