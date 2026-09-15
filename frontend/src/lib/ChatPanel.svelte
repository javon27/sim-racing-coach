<script lang="ts">
  import { onMount, tick } from "svelte";
  import { CoachStream, type CoachPhase, type ToolActivity } from "./coach";
  import { getChatMessages, getChatUsage, getSettings, type ChatUsage } from "./api";
  import { VoiceLoop, type VoiceMode, type VoiceState } from "./voice";
  import { MicRecorder, HttpTranscriber, SynthSpeaker } from "./audio";

  let {
    sessionId,
    lapId,
    chatId,
    onnewsession,
    stream = new CoachStream(),
    voice,
  }: {
    sessionId?: string;
    lapId?: number;
    // undefined = show the latest chat; null = a session with no chat was opened
    // (show an empty new chat); string = open that chat.
    chatId?: string | null;
    onnewsession?: () => void;
    stream?: {
      phase: CoachPhase;
      toolActivity: ToolActivity | null;
      answer: string;
      spoken?: string | null;
      chatId: string | null;
      error: string | null;
      usage?: ChatUsage | null;
      cost?: number | null;
      costComplete?: boolean;
      longChat?: boolean;
      ask(question: string, opts: { sessionId?: string; lapId?: number; chatId?: string; voice?: VoiceMode }): void;
      subscribe(run: (value: unknown) => void): () => void;
    };
    // Injected in tests (built on fakes). Left unset in the real app: ChatPanel
    // builds its own from Settings once they resolve (null = voice off / unsupported).
    voice?: VoiceLoop | null;
  } = $props();

  type Msg = { role: "you" | "coach"; text: string };

  // Minimal markdown -> HTML for coach answers: headings (#/##), "- " lists,
  // **bold**, `code`. No dependency — everything else renders as plain text.
  function escapeHtml(s: string): string {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function inline(s: string): string {
    return escapeHtml(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
  }
  function renderMarkdown(text: string): string {
    let html = "";
    let listItems: string[] = [];
    let paraLines: string[] = [];
    const flushList = () => {
      if (listItems.length) {
        html += `<ul>${listItems.map((li) => `<li>${inline(li)}</li>`).join("")}</ul>`;
        listItems = [];
      }
    };
    const flushPara = () => {
      if (paraLines.length) {
        html += `<p>${inline(paraLines.join(" "))}</p>`;
        paraLines = [];
      }
    };
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (line === "") {
        flushPara();
        flushList();
      } else if (/^#{1,2}\s+/.test(line)) {
        flushPara();
        flushList();
        html += `<h4>${inline(line.replace(/^#{1,2}\s+/, ""))}</h4>`;
      } else if (line.startsWith("- ")) {
        flushPara();
        listItems.push(line.slice(2));
      } else {
        flushList();
        paraLines.push(line);
      }
    }
    flushPara();
    flushList();
    return html;
  }

  let messages: Msg[] = $state([]);
  // The thread currently shown/appended to. undefined = a fresh, unsaved chat:
  // the next ask sends no chat_id, and we adopt the id the server mints.
  let currentChatId: string | undefined = $state(undefined);
  let question = $state("");
  let waiting = $state(false);
  let phase: CoachPhase = $state("idle");
  let toolActivity: ToolActivity | null = $state(null);
  let answer = $state("");
  let error: string | null = $state(null);
  let scrollEl: HTMLDivElement | undefined = $state();

  // Usage/cost meter. Every number here is computed by the core and handed over —
  // this component formats, it never prices.
  let usedTokens = $state(0);
  let cost: number | null = $state(null);
  let costComplete = $state(true);
  // Settings `show_cost` hides only the dollar figure; tokens always show. Read the
  // same way CornerPanel reads `units` — defaults on, so a settings hiccup shows it.
  let showCost = $state(true);

  // --- voice (#104) ---
  let activeVoice: VoiceLoop | null = $state(null);
  let pttKey = $state("F9");
  let voiceState: VoiceState = $state("idle");
  let speakingMessage: number | null = $state(null);
  let holdTimer: ReturnType<typeof setTimeout> | null = null;

  async function loadVoice() {
    // Set synchronously (before the await below) so an injected voice prop is
    // already wired by the time a synchronous test assertion runs post-render.
    if (voice !== undefined) activeVoice = voice ?? null;
    try {
      const settings = await getSettings();
      pttKey = settings.voice?.ptt_key ?? "F9";
      if (voice !== undefined) return; // prop wins -- tests inject a VoiceLoop on fakes
      const v = settings.voice;
      if (!v?.enabled || typeof navigator === "undefined" || !navigator.mediaDevices || !SynthSpeaker.available()) {
        activeVoice = null;
        return;
      }
      const speaker = new SynthSpeaker();
      speaker.voiceName = v.tts_voice;
      speaker.rate = v.tts_rate;
      activeVoice = new VoiceLoop({
        recorder: new MicRecorder(),
        transcriber: new HttpTranscriber(),
        speaker,
        // The actual send happens from the voice-state subscribe below (keyed on
        // lastQuestion) so an injected test loop drives the same hookup -- this
        // callback exists only because VoiceLoopOptions requires one.
        ask: () => {},
        vad: { threshold: v.vad_threshold, silenceMs: v.vad_silence_ms, maxS: v.vad_max_s },
        conversationIdleS: v.conversation_idle_s,
      });
    } catch {
      if (voice === undefined) activeVoice = null;
    }
  }

  let lastSentQuestion: string | null = null;
  $effect(() => {
    const v = activeVoice;
    if (!v) {
      voiceState = "idle";
      speakingMessage = null;
      lastSentQuestion = null;
      return;
    }
    return v.subscribe((s) => {
      voiceState = s.state;
      speakingMessage = s.speakingMessage;
      // Learned purely from public state (see VoiceLoop.lastQuestion) so this
      // fires the same way whether ChatPanel built the loop or a caller/test
      // injected one with its own `ask` wiring.
      if (s.state === "asking" && s.lastQuestion && s.lastQuestion !== lastSentQuestion) {
        lastSentQuestion = s.lastQuestion;
        sendVoice(s.lastQuestion, s.mode ?? "ptt");
      }
    });
  });

  function sendVoice(text: string, mode: VoiceMode) {
    messages = [...messages, { role: "you", text: "🎤 " + text }];
    waiting = true;
    stream.ask(text, { sessionId, lapId, chatId: currentChatId, voice: mode });
  }

  function micLabel(): string {
    switch (voiceState) {
      case "listening": return "Listening…";
      case "transcribing": return "Transcribing…";
      case "asking": return "Asking…";
      case "speaking": return "Stop speaking";
      default: return "Hold to talk, tap for conversation";
    }
  }

  function onMicDown() {
    if (!activeVoice) return;
    if (voiceState === "speaking") { activeVoice.stop(); return; }
    // ponytail: defer press() past the hold-vs-tap threshold so a quick tap can
    // start a persisted conversation listen (toggleConversation, below) instead
    // of firing -- and immediately having to abort -- a PTT release. Costs the
    // first ~250ms of audio on a genuine hold; revisit if that's ever felt.
    holdTimer = setTimeout(() => { holdTimer = null; activeVoice?.press(); }, 250);
  }

  function onMicUp() {
    if (!activeVoice) return;
    if (holdTimer) {
      clearTimeout(holdTimer);
      holdTimer = null;
      activeVoice.toggleConversation();
      return;
    }
    activeVoice.release();
  }

  function onWindowKeydown(e: KeyboardEvent) {
    if (!activeVoice || e.code !== pttKey || e.repeat) return;
    if (e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement) return;
    activeVoice.press();
  }

  function onWindowKeyup(e: KeyboardEvent) {
    if (!activeVoice || e.code !== pttKey) return;
    if (e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement) return;
    activeVoice.release();
  }

  function formatTokens(n: number): string {
    return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
  }

  // A dash for "no dollar figure to show": either the model could not be priced
  // (cost === null) or it is genuinely free (cost === 0, e.g. a local model).
  // "$0.00" would read as "this paid thing cost nothing", so it is never printed —
  // a real-but-tiny cost shows as "<$0.01" instead.
  function formatCost(c: number | null, complete: boolean): string {
    if (c === null || c === 0) return "—";
    const prefix = complete ? "" : "≥ ";
    if (c < 0.01) return `${prefix}<$0.01`;
    return `${prefix}$${c < 1 ? c.toFixed(3) : c.toFixed(2)}`;
  }

  // Soft long-chat nudge. The server decides WHEN (it owns the threshold); this only
  // renders it, and dismissing sticks for the rest of the thread so it never nags.
  let longChat = $state(false);
  let nudgeDismissed = $state(false);

  function clearMeter() {
    usedTokens = 0;
    cost = null;
    costComplete = true;
    longChat = false;
    nudgeDismissed = false;
  }

  // Keep the newest message/streaming text in view, unless the user has
  // scrolled up to read history (within 80px of bottom = "at the bottom").
  $effect(() => {
    messages.length;
    answer;
    const el = scrollEl;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  });

  // Populate the thread from a chat's saved messages.
  async function loadThread(id: string) {
    currentChatId = id;
    try {
      const rows = await getChatMessages(id);
      const hist: Msg[] = [];
      for (const row of rows) {
        hist.push({ role: "you", text: row.question });
        hist.push({ role: "coach", text: row.answer });
      }
      messages = hist;
    } catch {
      messages = [];
    }
    // Seed the meter so reopening a thread shows what it has already cost.
    // Best-effort, same shape as above: a sidecar hiccup hides the meter, it
    // never costs the driver the thread.
    try {
      const u = await getChatUsage(id);
      usedTokens = u.usage.total;
      cost = u.cost;
      costComplete = u.cost_complete;
    } catch {
      clearMeter();
    }
  }

  function newChat() {
    currentChatId = undefined;
    messages = [];
    clearMeter();
  }

  // "New session": clear the chat locally and ask the app to clear the selected
  // session, laps and track map so the driver starts fresh.
  function newSession() {
    newChat();
    onnewsession?.();
  }

  // Follow the chatId prop: a pinned id opens that thread; otherwise (null = a
  // session with no chat, or undefined = cold app load) start an EMPTY new chat.
  // We don't auto-open an old chat — that surprised the driver at the start of a
  // new session; the switcher dropdown still lets them resume any past chat.
  $effect(() => {
    const pinned = chatId;
    if (pinned) {
      loadThread(pinned);
    } else {
      newChat();
    }
  });

  // Read the Settings toggle the same way CornerPanel reads `units`. Best-effort:
  // the sidecar may not be up yet, so any failure keeps the default (show it).
  async function loadShowCost() {
    try {
      showCost = (await getSettings()).show_cost !== false;
    } catch {
      /* keep the default */
    }
  }

  onMount(() => {
    loadShowCost();
    loadVoice();
    window.addEventListener("settings-changed", loadVoice);
    window.addEventListener("keydown", onWindowKeydown);
    window.addEventListener("keyup", onWindowKeyup);
    const unsubscribe = stream.subscribe((s) => {
      const snap = s as { phase: CoachPhase; toolActivity: ToolActivity | null; answer: string; spoken?: string | null; chatId: string | null; error: string | null; usage?: ChatUsage | null; cost?: number | null; costComplete?: boolean; longChat?: boolean };
      phase = snap.phase;
      toolActivity = snap.toolActivity;
      answer = snap.answer;
      error = snap.error;
      if (snap.phase === "done") {
        messages = [...messages, { role: "coach", text: snap.answer }];
        waiting = false;
        // Adopt the server's chat id so follow-ups append to this thread.
        if (snap.chatId) currentChatId = snap.chatId;
        // ...and its cumulative usage snapshot (the persisted chat total).
        if (snap.usage) {
          usedTokens = snap.usage.total;
          cost = snap.cost ?? null;
          costComplete = snap.costComplete ?? true;
          longChat = snap.longChat === true;
        }
        activeVoice?.onAnswer(snap.answer, snap.spoken ?? null);
      } else if (snap.phase === "error") {
        const message = snap.error ?? "Something went wrong.";
        messages = [...messages, { role: "coach", text: message }];
        waiting = false;
        activeVoice?.onError(message);
      }
    });
    return () => {
      unsubscribe();
      window.removeEventListener("settings-changed", loadVoice);
      window.removeEventListener("keydown", onWindowKeydown);
      window.removeEventListener("keyup", onWindowKeyup);
    };
  });

  async function send() {
    const q = question.trim();
    if (!q || waiting) return;
    messages = [...messages, { role: "you", text: q }];
    question = "";
    waiting = true;
    stream.ask(q, { sessionId, lapId, chatId: currentChatId });
    // The user just acted — always snap their message into view, even if they'd
    // scrolled up (the nearBottom effect above then follows the streamed reply).
    await tick();
    if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function autoGrow(e: Event) {
    const el = e.target as HTMLTextAreaElement;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }
</script>

<section class="chat">
  <div class="chat-bar">
    {#if usedTokens > 0}
      <span
        class="meter"
        data-testid="usage-meter"
        title="Tokens billed in this chat, and the estimated API cost of them. Excludes setup capture, track-notes drafting, and memory operations, which are billed separately to your key."
      >{formatTokens(usedTokens)} tokens{showCost ? ` · ${formatCost(cost, costComplete)}` : ""}</span>
    {/if}
    <button type="button" class="new-chat" onclick={newSession}>+ New session</button>
  </div>
  {#if longChat && !nudgeDismissed}
    <!-- Soft nudge only: nothing is summarized or truncated. Prior sessions stay
         reachable through the coach's own history tools, so a reset loses nothing
         it cannot re-fetch. -->
    <div class="nudge" data-testid="long-chat-nudge" role="status">
      <span>Long chat — start a New chat to keep it snappy.</span>
      <button type="button" class="nudge-go" onclick={newChat}>Start a new chat</button>
      <button
        type="button"
        class="nudge-x"
        aria-label="Dismiss"
        onclick={() => (nudgeDismissed = true)}
      >×</button>
    </div>
  {/if}
  <div class="chat-scroll" bind:this={scrollEl}>
    <div class="thread">
      {#if messages.length === 0 && !waiting}
        <p class="chat-empty">New chat — ask a question</p>
      {/if}
      {#each messages as m, i (i)}
        <div class="msg {m.role}">
          <div class="who">
            {#if m.role === "you"}
              <!-- Helmet avatar (the bubble's "You" name already labels the sender, so
                   a "YOU" badge here doubled it). Profile-image customization: backlog. -->
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-label="You"><path d="M4 13a8 8 0 0 1 16 0v2a2 2 0 0 1-2 2h-6l-2 3-1.6-3H6a2 2 0 0 1-2-2z"/><path d="M8 12h9"/></svg>
            {:else}⚑{/if}
          </div>
          <div class="bubble">
            <div class="name">
              {m.role === "you" ? "You" : "Engineer"}
              {#if activeVoice && m.role === "coach"}
                <button
                  type="button"
                  class="play"
                  aria-label={speakingMessage === i ? "Stop" : "Read aloud"}
                  aria-pressed={speakingMessage === i}
                  onclick={() => activeVoice?.speakMessage(i, m.text)}
                >{speakingMessage === i ? "■" : "▶"}</button>
              {/if}
            </div>
            {#if m.role === "coach"}
              <div class="body md">{@html renderMarkdown(m.text)}</div>
            {:else}
              <div class="body">{m.text}</div>
            {/if}
          </div>
        </div>
      {/each}
      {#if waiting}
        <div class="msg coach">
          <div class="who">⚑</div>
          <div class="bubble">
            <div class="name">Engineer</div>
            <div class="body md">
              {#if phase === "tool"}
                <span class="typing">reading your lap{toolActivity ? ` (${toolActivity.name})` : ""}…</span>
              {:else if phase === "streaming"}
                {@html renderMarkdown(answer)}
              {:else}
                <span class="typing">thinking…</span>
              {/if}
            </div>
          </div>
        </div>
      {/if}
    </div>
  </div>
  <div class="composer">
    <div class="wrap">
      <textarea
        rows="1"
        aria-label="Ask your engineer"
        placeholder="Ask your engineer…  (e.g. where am I losing time?)"
        bind:value={question}
        oninput={autoGrow}
        onkeydown={onKeydown}
      ></textarea>
      {#if activeVoice}
        <button
          type="button"
          class="mic"
          aria-label={micLabel()}
          data-voice-state={voiceState}
          onpointerdown={onMicDown}
          onpointerup={onMicUp}
          onpointerleave={onMicUp}
        >{voiceState === "speaking" ? "■" : "🎤"}</button>
      {/if}
      <button type="button" class="send" onclick={send} disabled={waiting}>Send</button>
    </div>
  </div>
</section>

<style>
  .chat-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--line);
  }
  .meter {
    margin-left: auto;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
    white-space: nowrap;
  }
  /* With a meter present the meter takes the auto margin; alone, the button does. */
  .meter + .new-chat {
    margin-left: 0;
  }
  .new-chat {
    margin-left: auto;
    font-family: var(--body);
    font-size: 12px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 5px 11px;
    cursor: pointer;
  }
  .new-chat:hover {
    border-color: var(--accent);
  }
  .nudge {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
    border-bottom: 1px solid var(--line);
  }
  .nudge-go {
    font-family: var(--body);
    font-size: 11px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 3px 9px;
    cursor: pointer;
  }
  .nudge-go:hover {
    border-color: var(--accent);
  }
  .nudge-x {
    margin-left: auto;
    font-size: 14px;
    line-height: 1;
    color: var(--faint);
    background: none;
    border: none;
    padding: 0 2px;
    cursor: pointer;
  }
  .nudge-x:hover {
    color: var(--text);
  }
  .mic {
    font-size: 16px;
    line-height: 1;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 10px;
    cursor: pointer;
    touch-action: none;
  }
  .mic:hover {
    border-color: var(--accent);
  }
  .mic[data-voice-state="listening"] {
    border-color: var(--accent);
    animation: mic-pulse 1.2s ease-in-out infinite;
  }
  @keyframes mic-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }
  .play {
    margin-left: 6px;
    font-size: 10px;
    line-height: 1;
    color: var(--faint);
    background: none;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 1px 5px;
    cursor: pointer;
  }
  .play:hover {
    color: var(--text);
    border-color: var(--accent);
  }
  .chat-empty {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
    text-align: center;
    margin: 24px 0;
  }
</style>
