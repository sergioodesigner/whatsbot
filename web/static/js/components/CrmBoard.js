import { h } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';
import htm from 'htm';
import { createCrmTask, getCrmBoard, getCrmTasks, updateCrmDeal, updateCrmTask, upsertCrmDeal } from '../services/api.js';

const html = htm.bind(h);

const STAGE_LABELS = {
  novo: 'Novo',
  em_atendimento: 'Em atendimento',
  proposta: 'Proposta',
  fechado_ganho: 'Fechado (ganho)',
  perdido: 'Perdido',
};

function dateBr(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('pt-BR');
}

function originLabel(origin) {
  const raw = String(origin || 'manual');
  if (raw === 'whatsapp_auto') return 'WhatsApp (automático)';
  if (raw.startsWith('manual:')) return `Manual (${raw.slice(7) || 'outra origem'})`;
  if (raw === 'manual') return 'Manual';
  return raw;
}

export function CrmBoard() {
  const [stages, setStages] = useState([]);
  const [dealsByStage, setDealsByStage] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [phone, setPhone] = useState('');
  const [title, setTitle] = useState('');
  const [owner, setOwner] = useState('');
  const [value, setValue] = useState('');
  const [origin, setOrigin] = useState('');
  const [selectedDeal, setSelectedDeal] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [taskTitle, setTaskTitle] = useState('');

  async function loadBoard() {
    setLoading(true);
    setError('');
    const res = await getCrmBoard();
    if (!res.ok) {
      setError(res.error || 'Falha ao carregar CRM.');
      setLoading(false);
      return;
    }
    setStages(res.data?.stages || []);
    setDealsByStage(res.data?.deals_by_stage || {});
    setLoading(false);
  }

  useEffect(() => {
    loadBoard();
  }, []);

  const allDeals = useMemo(
    () => stages.flatMap((s) => dealsByStage[s] || []),
    [stages, dealsByStage],
  );

  async function handleCreateDeal(e) {
    e.preventDefault();
    const normalized = phone.replace(/\D/g, '');
    if (!normalized) return;
    const payload = {
      contact_phone: normalized.startsWith('55') ? normalized : `55${normalized}`,
      title: title.trim(),
      owner: owner.trim(),
      potential_value: parseFloat(value || '0') || 0,
      stage: 'novo',
      origin: origin.trim() ? `manual:${origin.trim().toLowerCase()}` : 'manual',
    };
    const res = await upsertCrmDeal(payload);
    if (!res.ok) return setError(res.error || 'Erro ao criar oportunidade.');
    setPhone('');
    setTitle('');
    setOwner('');
    setValue('');
    setOrigin('');
    await loadBoard();
  }

  async function moveDeal(deal, stage) {
    const res = await updateCrmDeal(deal.id, { stage });
    if (!res.ok) return setError(res.error || 'Erro ao mover oportunidade.');
    await loadBoard();
    if (selectedDeal && selectedDeal.id === deal.id) {
      setSelectedDeal({ ...selectedDeal, stage });
    }
  }

  async function openDeal(deal) {
    setSelectedDeal(deal);
    const res = await getCrmTasks(deal.id);
    if (res.ok) setTasks(res.data?.tasks || []);
  }

  async function createTask(e) {
    e.preventDefault();
    if (!selectedDeal || !taskTitle.trim()) return;
    const res = await createCrmTask(selectedDeal.id, { title: taskTitle.trim() });
    if (!res.ok) return setError(res.error || 'Erro ao criar tarefa.');
    setTaskTitle('');
    const fresh = await getCrmTasks(selectedDeal.id);
    if (fresh.ok) setTasks(fresh.data?.tasks || []);
  }

  async function toggleTask(task) {
    const res = await updateCrmTask(task.id, { done: !task.done });
    if (!res.ok) return setError(res.error || 'Erro ao atualizar tarefa.');
    const fresh = await getCrmTasks(selectedDeal.id);
    if (fresh.ok) setTasks(fresh.data?.tasks || []);
  }

  if (loading) return html`<div class="text-sm text-wa-secondary">Carregando CRM...</div>`;
  if (error && !stages.length) return html`<div class="text-sm text-red-600">${error}</div>`;

  return html`
    <div class="space-y-4">
      <form onSubmit=${handleCreateDeal} class="bg-white rounded-xl p-4 border border-wa-border grid grid-cols-1 md:grid-cols-6 gap-2">
        <input value=${phone} onInput=${(e) => setPhone(e.target.value)} class="border rounded px-3 py-2" placeholder="Telefone (com DDD)" />
        <input value=${title} onInput=${(e) => setTitle(e.target.value)} class="border rounded px-3 py-2" placeholder="Título da oportunidade" />
        <input value=${owner} onInput=${(e) => setOwner(e.target.value)} class="border rounded px-3 py-2" placeholder="Responsável" />
        <input value=${value} onInput=${(e) => setValue(e.target.value)} type="number" step="0.01" class="border rounded px-3 py-2" placeholder="Valor potencial" />
        <input value=${origin} onInput=${(e) => setOrigin(e.target.value)} class="border rounded px-3 py-2" placeholder="Origem manual (ex.: instagram)" />
        <button class="bg-wa-teal text-white rounded px-3 py-2">Adicionar no funil</button>
      </form>

      ${error ? html`<div class="text-sm text-red-600">${error}</div>` : null}

      <div class="grid grid-cols-1 md:grid-cols-5 gap-3">
        ${stages.map((stage) => html`
          <div class="bg-white rounded-xl border border-wa-border p-3">
            <div class="font-semibold text-sm mb-2">${STAGE_LABELS[stage] || stage}</div>
            <div class="space-y-2">
              ${(dealsByStage[stage] || []).map((deal) => html`
                <button onClick=${() => openDeal(deal)} class="w-full text-left border rounded p-2 hover:bg-wa-hover">
                  <div class="text-sm font-medium">${deal.title || deal.contact?.name || deal.contact_phone}</div>
                  <div class="text-xs text-wa-secondary">${deal.contact?.phone || deal.contact_phone}</div>
                  <div class="text-xs text-wa-secondary">R$ ${Number(deal.potential_value || 0).toFixed(2)}</div>
                  <div class="text-xs mt-1">
                    <span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">${originLabel(deal.origin)}</span>
                  </div>
                  <div class="mt-2">
                    <select class="text-xs border rounded px-2 py-1" value=${deal.stage} onChange=${(e) => moveDeal(deal, e.target.value)}>
                      ${stages.map((s) => html`<option value=${s}>${STAGE_LABELS[s] || s}</option>`)}
                    </select>
                  </div>
                </button>
              `)}
            </div>
          </div>
        `)}
      </div>

      ${selectedDeal ? html`
        <div
          class="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4"
          onClick=${(e) => { if (e.target === e.currentTarget) setSelectedDeal(null); }}
        >
          <div class="bg-white rounded-xl border border-wa-border w-full max-w-3xl max-h-[85vh] overflow-auto p-4 space-y-3">
            <div class="flex items-center justify-between">
              <h3 class="font-semibold">Oportunidade: ${selectedDeal.title || selectedDeal.contact?.name || selectedDeal.contact_phone}</h3>
              <button class="text-sm text-wa-secondary hover:underline" onClick=${() => setSelectedDeal(null)}>Fechar</button>
            </div>
            <div class="text-sm text-wa-secondary">
              Contato: ${selectedDeal.contact?.name || '-'} (${selectedDeal.contact?.phone || selectedDeal.contact_phone})
            </div>
            <div class="text-sm text-wa-secondary">
              Origem: ${originLabel(selectedDeal.origin)}
            </div>
            <div class="text-sm text-wa-secondary">
              Observações do contato: ${(selectedDeal.contact?.observations || []).slice(0, 2).join(' | ') || 'Sem observações'}
            </div>
            <form onSubmit=${createTask} class="flex gap-2">
              <input value=${taskTitle} onInput=${(e) => setTaskTitle(e.target.value)} class="border rounded px-3 py-2 flex-1" placeholder="Nova tarefa" />
              <button class="border rounded px-3 py-2">Adicionar tarefa</button>
            </form>
            <div class="space-y-2">
              ${tasks.map((t) => html`
                <label class="flex items-center justify-between border rounded px-3 py-2 text-sm">
                  <span class=${t.done ? 'line-through text-wa-secondary' : ''}>${t.title}</span>
                  <button class="text-xs border rounded px-2 py-1" onClick=${() => toggleTask(t)} type="button">
                    ${t.done ? 'Reabrir' : 'Concluir'}
                  </button>
                </label>
              `)}
            </div>
            <div class="text-xs text-wa-secondary">
              Total de oportunidades no funil: ${allDeals.length}. Última atualização: ${dateBr(Date.now() / 1000)}.
            </div>
          </div>
        </div>
      ` : null}
    </div>
  `;
}
