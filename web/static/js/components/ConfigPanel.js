import { h } from 'preact';
import { useState, useEffect } from 'preact/hooks';
import htm from 'htm';
import { getBillingInvoices } from '../services/api.js';

const html = htm.bind(h);

function Section({ title, children }) {
  return html`
    <div class="bg-white rounded-xl p-5 border border-wa-border shadow-sm">
      ${title ? html`
        <h3 class="text-xs font-semibold text-wa-secondary uppercase tracking-wider mb-4">${title}</h3>
      ` : null}
      <div class="flex flex-col gap-4">
        ${children}
      </div>
    </div>
  `;
}

export function ConfigPanel({ config, saving, onSave, onNotify }) {
  const [systemPrompt, setSystemPrompt] = useState('');
  const [autoReply, setAutoReply] = useState(true);
  const [maxContext, setMaxContext] = useState(10);
  const [batchDelay, setBatchDelay] = useState(3);
  const [splitMessages, setSplitMessages] = useState(true);
  const [splitDelay, setSplitDelay] = useState(2);
  const [audioTranscriptionEnabled, setAudioTranscriptionEnabled] = useState(true);
  const [imageTranscriptionEnabled, setImageTranscriptionEnabled] = useState(true);
  const [transferAlertEnabled, setTransferAlertEnabled] = useState(true);
  const [transferAlertDuration, setTransferAlertDuration] = useState(5);
  const [defaultAiEnabled, setDefaultAiEnabled] = useState(true);
  const [aiReplyTriggerPhrase, setAiReplyTriggerPhrase] = useState('');
  const [aiSessionEndPhrase, setAiSessionEndPhrase] = useState('');
  const [selfChatUserPrefix, setSelfChatUserPrefix] = useState('');
  const [aiEndSessionDisablesAi, setAiEndSessionDisablesAi] = useState(true);
  const [webPassword, setWebPassword] = useState('');
  const [webPasswordConfirm, setWebPasswordConfirm] = useState('');
  const [removePassword, setRemovePassword] = useState(false);

  const [saveSuccess, setSaveSuccess] = useState(false);
  const [promptFullscreen, setPromptFullscreen] = useState(false);
  const [invoices, setInvoices] = useState([]);
  const [billingLoading, setBillingLoading] = useState(false);

  function formatDateBr(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    const day = String(d.getDate()).padStart(2, '0');
    const month = d.toLocaleString('pt-BR', { month: 'long' }).toLowerCase();
    return `${day}-${month}-${d.getFullYear()}`;
  }

  function invoiceBadge(invoice) {
    if (invoice.paid) return html`<span class="px-2 py-0.5 rounded bg-green-100 text-green-700 text-xs font-semibold">Pago</span>`;
    if ((invoice.due_ts || 0) * 1000 < Date.now()) return html`<span class="px-2 py-0.5 rounded bg-red-100 text-red-700 text-xs font-semibold">Atrasado</span>`;
    return html`<span class="px-2 py-0.5 rounded bg-yellow-100 text-yellow-700 text-xs font-semibold">Pendente</span>`;
  }

  // Populate form when config loads
  useEffect(() => {
    if (config) {
      setSystemPrompt(config.system_prompt || '');
      setAutoReply(config.auto_reply ?? true);
      setMaxContext(config.max_context_messages ?? 10);
      setBatchDelay(config.message_batch_delay ?? 3);
      setSplitMessages(config.split_messages ?? true);
      setSplitDelay(config.split_message_delay ?? 2);
      setAudioTranscriptionEnabled(config.audio_transcription_enabled ?? true);
      setImageTranscriptionEnabled(config.image_transcription_enabled ?? true);
      setTransferAlertEnabled(config.transfer_alert_enabled ?? true);
      setTransferAlertDuration(config.transfer_alert_duration ?? 5);
      setDefaultAiEnabled(config.default_ai_enabled ?? true);
      setAiReplyTriggerPhrase(config.ai_reply_trigger_phrase ?? '');
      setAiSessionEndPhrase(config.ai_session_end_phrase ?? '');
      setSelfChatUserPrefix(config.self_chat_user_prefix ?? '');
      setAiEndSessionDisablesAi(config.ai_end_session_disables_ai ?? true);
    }
  }, [config]);

  useEffect(() => {
    let cancelled = false;
    async function loadInvoices() {
      setBillingLoading(true);
      const res = await getBillingInvoices();
      if (!cancelled) {
        setInvoices(res?.ok ? (res.data?.invoices || []) : []);
        setBillingLoading(false);
      }
    }
    loadInvoices();
    return () => { cancelled = true; };
  }, []);

  async function handleSave() {
    const data = {
      system_prompt: systemPrompt,
      auto_reply: autoReply,
      max_context_messages: parseInt(maxContext, 10) || 10,
      message_batch_delay: isNaN(parseFloat(batchDelay)) ? 0 : parseFloat(batchDelay),
      split_messages: splitMessages,
      split_message_delay: isNaN(parseFloat(splitDelay)) ? 0 : parseFloat(splitDelay),
      audio_transcription_enabled: audioTranscriptionEnabled,
      image_transcription_enabled: imageTranscriptionEnabled,
      transfer_alert_enabled: transferAlertEnabled,
      transfer_alert_duration: parseInt(transferAlertDuration, 10) || 5,
      default_ai_enabled: defaultAiEnabled,
      ai_reply_trigger_phrase: aiReplyTriggerPhrase.trim(),
      ai_session_end_phrase: aiSessionEndPhrase.trim(),
      self_chat_user_prefix: selfChatUserPrefix.trim(),
      ai_end_session_disables_ai: aiEndSessionDisablesAi,
    };
    // Handle password change/removal
    if (removePassword) {
      data.web_password = '';
    } else if (webPassword.trim()) {
      if (webPassword !== webPasswordConfirm) {
        onNotify('As senhas não coincidem.');
        return;
      }
      data.web_password = webPassword;
    }
    setSaveSuccess(false);
    const result = await onSave(data);
    if (result !== false) {
      setSaveSuccess(true);
      setWebPassword('');
      setWebPasswordConfirm('');
      setRemovePassword(false);
      setTimeout(() => setSaveSuccess(false), 3000);
    }
  }

  if (!config) {
    return html`<div class="bg-white rounded-xl p-5 animate-pulse-slow text-wa-secondary border border-wa-border">Carregando...</div>`;
  }

  return html`
    <div class="flex flex-col gap-4 flex-1">

      <!-- Section: Automacao -->
      <${Section} title="Automação">
        <label class="flex items-center gap-3 text-sm font-semibold text-wa-text cursor-pointer p-3 rounded-lg border ${autoReply ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}">
          <input
            type="checkbox"
            checked=${autoReply}
            onChange=${(e) => setAutoReply(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          Ativar agente de IA para responder mensagens
        </label>

        <label class="flex items-center gap-3 text-sm font-semibold text-wa-text cursor-pointer p-3 rounded-lg border ${defaultAiEnabled ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}">
          <input
            type="checkbox"
            checked=${defaultAiEnabled}
            onChange=${(e) => setDefaultAiEnabled(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          IA ativada por padrão para novos contatos
        </label>
      <//>

      <!-- Section: Gatilhos -->
      <${Section} title="Gatilhos de conversa">
        <p class="text-xs text-wa-secondary leading-relaxed">
          Use uma <strong class="text-wa-text font-medium">frase de ativação</strong> para a IA responder
          apenas quando a mensagem agrupada começar com esse texto.
          Uma <strong class="text-wa-text font-medium">frase de encerramento</strong> gera despedida da IA
          e pode desativar o agente no contato.
          No <strong class="text-wa-text font-medium">chat consigo mesmo</strong>, você pode usar um prefixo
          próprio para tratar mensagens como entrada de usuário para a IA.
        </p>
        <div>
          <label class="block text-sm font-semibold text-wa-text mb-1">Frase de ativação (opcional)</label>
          <input
            type="text"
            value=${aiReplyTriggerPhrase}
            onInput=${(e) => setAiReplyTriggerPhrase(e.target.value)}
            placeholder="Ex.: !bot ou #ia"
            class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none font-mono"
          />
          <span class="text-xs text-wa-secondary">Deixe vazio para responder qualquer mensagem.</span>
        </div>
        <div>
          <label class="block text-sm font-semibold text-wa-text mb-1">Frase de encerramento (opcional)</label>
          <input
            type="text"
            value=${aiSessionEndPhrase}
            onInput=${(e) => setAiSessionEndPhrase(e.target.value)}
            placeholder="Ex.: encerrar atendimento"
            class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
          />
          <span class="text-xs text-wa-secondary">A mensagem precisa ser exatamente esta frase (ignora maiúsculas/minúsculas).</span>
        </div>
        <label class="flex items-center gap-3 text-sm font-semibold text-wa-text cursor-pointer p-3 rounded-lg border ${aiEndSessionDisablesAi ? 'bg-green-50 border-green-200' : 'bg-wa-panel border-wa-border'}">
          <input
            type="checkbox"
            checked=${aiEndSessionDisablesAi}
            onChange=${(e) => setAiEndSessionDisablesAi(e.target.checked)}
            class="w-4 h-4 rounded border-wa-border accent-wa-teal"
          />
          Ao encerrar, desativar IA neste contato
        </label>
        <div>
          <label class="block text-sm font-semibold text-wa-text mb-1">Prefixo no chat comigo mesmo (opcional)</label>
          <input
            type="text"
            value=${selfChatUserPrefix}
            onInput=${(e) => setSelfChatUserPrefix(e.target.value)}
            placeholder="Ex.: #bot "
            class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none font-mono"
          />
          <span class="text-xs text-wa-secondary">No seu próprio número, mensagens com esse prefixo viram entrada de usuário para a IA.</span>
        </div>
      <//>

      <!-- Section: System Prompt -->
      <${Section} title="System Prompt">
        <div class="flex-1 flex flex-col">
          <div class="flex items-center justify-between mb-1">
            <label class="block text-sm font-semibold text-wa-text">Prompt</label>
            <button
              type="button"
              onClick=${() => setPromptFullscreen(true)}
              class="text-wa-secondary hover:text-wa-teal transition-colors p-1 rounded"
              title="Abrir editor em tela cheia"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
            </button>
          </div>
          <textarea
            value=${systemPrompt}
            onInput=${(e) => setSystemPrompt(e.target.value)}
            rows="4"
            class="w-full flex-1 bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none resize-none"
          ></textarea>
        </div>
      <//>

      <!-- Fullscreen Prompt Editor -->
      ${promptFullscreen ? html`
        <div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick=${(e) => { if (e.target === e.currentTarget) setPromptFullscreen(false); }}>
          <div class="bg-white w-full h-full rounded-xl flex flex-col shadow-2xl overflow-hidden">
            <div class="flex items-center justify-between px-5 py-3 border-b border-wa-border">
              <h2 class="text-sm font-semibold text-wa-text">System Prompt</h2>
              <button
                type="button"
                onClick=${() => setPromptFullscreen(false)}
                class="text-wa-secondary hover:text-wa-text transition-colors p-1 rounded"
                title="Fechar"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <textarea
              value=${systemPrompt}
              onInput=${(e) => setSystemPrompt(e.target.value)}
              class="flex-1 w-full bg-white text-wa-text px-5 py-4 text-sm leading-relaxed focus:outline-none resize-none"
              autofocus
            ></textarea>
          </div>
        </div>
      ` : null}

      <!-- Section: Comportamento -->
      <${Section} title="Comportamento">
        <!-- Context & Batch Settings -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-sm font-semibold text-wa-text mb-1">Mensagens de contexto</label>
            <input
              type="number"
              min="2"
              max="100"
              value=${maxContext}
              onInput=${(e) => setMaxContext(e.target.value)}
              class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            <span class="text-xs text-wa-secondary">Qtd de msgs enviadas ao LLM</span>
          </div>
          <div>
            <label class="block text-sm font-semibold text-wa-text mb-1">Agrupar mensagens (s)</label>
            <input
              type="number"
              min="0"
              max="30"
              step="0.5"
              value=${batchDelay}
              onInput=${(e) => setBatchDelay(e.target.value)}
              class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            <span class="text-xs text-wa-secondary">Espera antes de responder</span>
          </div>
        </div>

        <!-- Split Messages -->
        <div class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border">
          <label class="flex items-center gap-2 text-sm font-semibold text-wa-text cursor-pointer">
            <input
              type="checkbox"
              checked=${splitMessages}
              onChange=${(e) => setSplitMessages(e.target.checked)}
              class="w-4 h-4 rounded border-wa-border accent-wa-teal"
            />
            Mensagens picadas (dividir resposta)
          </label>
          <span class="text-xs text-wa-secondary">Divide a resposta da IA em várias mensagens curtas, como uma conversa natural</span>
          ${splitMessages ? html`
            <div class="mt-1">
              <label class="block text-xs font-medium text-wa-text mb-1">Intervalo entre mensagens (s)</label>
              <input
                type="number"
                min="0"
                max="10"
                step="0.5"
                value=${splitDelay}
                onInput=${(e) => setSplitDelay(e.target.value)}
                class="w-32 bg-white text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              />
            </div>
          ` : null}
        </div>

        <!-- Transfer Alert -->
        <div class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border">
          <label class="flex items-center gap-2 text-sm font-semibold text-wa-text cursor-pointer">
            <input
              type="checkbox"
              checked=${transferAlertEnabled}
              onChange=${(e) => setTransferAlertEnabled(e.target.checked)}
              class="w-4 h-4 rounded border-wa-border accent-wa-teal"
            />
            Alerta sonoro ao transferir para humano
          </label>
          <span class="text-xs text-wa-secondary">Emite um alerta sonoro quando a IA transfere o atendimento para um humano</span>
          ${transferAlertEnabled ? html`
            <div class="mt-1">
              <label class="block text-xs font-medium text-wa-text mb-1">Duração do alerta (segundos)</label>
              <input
                type="number"
                min="1"
                max="30"
                step="1"
                value=${transferAlertDuration}
                onInput=${(e) => setTransferAlertDuration(e.target.value)}
                class="w-32 bg-white text-wa-text px-3 py-1.5 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
              />
            </div>
          ` : null}
        </div>
      <//>

      <!-- Section: Avancado -->
      <${Section} title="Avançado">
        <!-- Panel Password -->
        <div class="flex flex-col gap-2 p-3 bg-wa-panel rounded-lg border border-wa-border">
          <div class="flex items-center justify-between">
            <label class="text-sm font-semibold text-wa-text">Senha do Painel</label>
            ${config.has_password ? html`
              <span class="text-xs bg-wa-teal text-white px-2 py-0.5 rounded-full">Ativa</span>
            ` : html`
              <span class="text-xs bg-wa-secondary/20 text-wa-secondary px-2 py-0.5 rounded-full">Desativada</span>
            `}
          </div>
          <span class="text-xs text-wa-secondary">Protege o acesso ao painel web com senha</span>
          ${!removePassword ? html`
            <input
              type="password"
              value=${webPassword}
              onInput=${(e) => setWebPassword(e.target.value)}
              placeholder=${config.has_password ? 'Nova senha (deixe vazio para manter)' : 'Definir senha'}
              class="w-full bg-white text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
            />
            ${webPassword ? html`
              <input
                type="password"
                value=${webPasswordConfirm}
                onInput=${(e) => setWebPasswordConfirm(e.target.value)}
                placeholder="Confirmar senha"
                class="w-full bg-white text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none ${webPassword && webPasswordConfirm && webPassword !== webPasswordConfirm ? 'border-red-400' : ''}"
              />
              ${webPassword && webPasswordConfirm && webPassword !== webPasswordConfirm ? html`
                <span class="text-xs text-red-500">As senhas não coincidem</span>
              ` : null}
            ` : null}
          ` : null}
          ${config.has_password ? html`
            <label class="flex items-center gap-2 text-sm text-red-600 cursor-pointer mt-1">
              <input
                type="checkbox"
                checked=${removePassword}
                onChange=${(e) => { setRemovePassword(e.target.checked); if (e.target.checked) { setWebPassword(''); setWebPasswordConfirm(''); } }}
                class="w-4 h-4 rounded border-wa-border accent-red-600"
              />
              Remover senha
            </label>
          ` : null}
        </div>

      <//>

      <${Section} title="Financeiro">
        ${billingLoading ? html`
          <div class="text-sm text-wa-secondary">Carregando faturas...</div>
        ` : invoices.length ? html`
          <div class="overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead>
                <tr class="border-b border-wa-border">
                  <th class="py-2 pr-3">Período</th>
                  <th class="py-2 pr-3">Vencimento</th>
                  <th class="py-2 pr-3">Valor</th>
                  <th class="py-2 pr-3">Status</th>
                </tr>
              </thead>
              <tbody>
                ${invoices.map((inv) => html`
                  <tr class="border-b border-wa-border/70">
                    <td class="py-2 pr-3 font-medium">${inv.period_ym || '-'}</td>
                    <td class="py-2 pr-3">${formatDateBr(inv.due_ts)}</td>
                    <td class="py-2 pr-3">R$ ${Number(inv.amount || 0).toFixed(2)}</td>
                    <td class="py-2 pr-3">${invoiceBadge(inv)}</td>
                  </tr>
                `)}
              </tbody>
            </table>
          </div>
        ` : html`
          <div class="text-sm text-wa-secondary">Nenhuma fatura disponível no momento.</div>
        `}
      <//>

      <!-- Save Button (sticky) -->
      <div class="sticky bottom-0 z-10 bg-wa-panel pt-2 pb-1">
        <button
          onClick=${handleSave}
          disabled=${saving}
          class="w-full py-2.5 ${saveSuccess ? 'bg-green-600' : 'bg-wa-teal hover:bg-wa-tealDark'} disabled:opacity-50 text-white font-medium rounded-lg transition-colors shadow-sm"
        >
          ${saving ? 'Salvando...' : saveSuccess ? '\u2713 Salvo!' : 'Salvar Configurações'}
        </button>
      </div>
    </div>
  `;
}
