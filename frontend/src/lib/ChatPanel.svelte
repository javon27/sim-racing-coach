<script lang="ts">
  import { onMount, tick } from "svelte";
  import { CoachStream, type CoachPhase, type ToolActivity } from "./coach";
  import { getChats, getChatMessages, type ChatRow } from "./api";

  let {
    sessionId,
    lapId,
    chatId,
    stream = new CoachStream(),
  }: {
    sessionId?: string;
    lapId?: number;
    // undefined = show the latest chat; null = a session with no chat was opened
    // (show an empty new chat); string = open that chat.
    chatId?: string | null;
    stream?: {
      phase: CoachPhase;
      toolActivity: ToolActivity | null;
      answer: string;
      chatId: string | null;
      error: string | null;
      ask(question: string, opts: { sessionId?: string; lapId?: number; chatId?: string }): void;
      subscribe(run: (value: unknown) => void): () => void;
    };
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
  let chats: ChatRow[] = $state([]);
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

  async function refreshChats() {
    try {
      chats = await getChats();
    } catch {
      // switcher stays with whatever it had
    }
  }

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
  }

  function newChat() {
    currentChatId = undefined;
    messages = [];
  }

  function onSwitch(e: Event) {
    const id = (e.target as HTMLSelectElement).value;
    if (id) loadThread(id);
  }

  function chatLabel(c: ChatRow): string {
    const when = new Date(c.last * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return `${c.tracks || "Chat"} · ${when}`;
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

  onMount(() => {
    refreshChats();
    const unsubscribe = stream.subscribe((s) => {
      const snap = s as { phase: CoachPhase; toolActivity: ToolActivity | null; answer: string; chatId: string | null; error: string | null };
      phase = snap.phase;
      toolActivity = snap.toolActivity;
      answer = snap.answer;
      error = snap.error;
      if (snap.phase === "done") {
        messages = [...messages, { role: "coach", text: snap.answer }];
        waiting = false;
        // Adopt the server's chat id so follow-ups append to this thread, and
        // surface a newly-created chat in the switcher.
        if (snap.chatId) currentChatId = snap.chatId;
        refreshChats();
      } else if (snap.phase === "error") {
        messages = [...messages, { role: "coach", text: snap.error ?? "Something went wrong." }];
        waiting = false;
      }
    });
    return unsubscribe;
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
    <select
      class="chat-switch"
      aria-label="Switch chat"
      value={currentChatId ?? ""}
      onchange={onSwitch}
    >
      {#if currentChatId == null}
        <option value="">New chat</option>
      {/if}
      {#each chats as c (c.chat_id)}
        <option value={c.chat_id}>{chatLabel(c)}</option>
      {/each}
    </select>
    <button type="button" class="new-chat" onclick={newChat}>+ New chat</button>
  </div>
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
            <div class="name">{m.role === "you" ? "You" : "Engineer"}</div>
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
  .chat-switch {
    font-family: var(--body);
    font-size: 12px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 5px 9px;
    max-width: 220px;
    cursor: pointer;
  }
  .chat-switch:focus {
    outline: none;
    border-color: var(--accent);
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
  .chat-empty {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
    text-align: center;
    margin: 24px 0;
  }
</style>
