<script lang="ts">
  import { onMount } from "svelte";
  import { CoachStream, type CoachPhase, type ToolActivity } from "./coach";
  import { getChats, getChatMessages } from "./api";

  let {
    sessionId,
    lapId,
    stream = new CoachStream(),
  }: {
    sessionId?: string;
    lapId?: number;
    stream?: {
      phase: CoachPhase;
      toolActivity: ToolActivity | null;
      answer: string;
      error: string | null;
      ask(question: string, opts: { sessionId?: string; lapId?: number }): void;
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
  let question = $state("");
  let waiting = $state(false);
  let phase: CoachPhase = $state("idle");
  let toolActivity: ToolActivity | null = $state(null);
  let answer = $state("");
  let error: string | null = $state(null);

  async function loadHistory() {
    try {
      const chats = await getChats();
      if (chats.length === 0) return;
      const rows = await getChatMessages(chats[0].chat_id);
      const hist: Msg[] = [];
      for (const row of rows) {
        hist.push({ role: "you", text: row.question });
        hist.push({ role: "coach", text: row.answer });
      }
      messages = hist;
    } catch {
      // no history available yet - an empty thread is fine
    }
  }

  onMount(() => {
    const unsubscribe = stream.subscribe((s) => {
      const snap = s as { phase: CoachPhase; toolActivity: ToolActivity | null; answer: string; error: string | null };
      phase = snap.phase;
      toolActivity = snap.toolActivity;
      answer = snap.answer;
      error = snap.error;
      if (snap.phase === "done") {
        messages = [...messages, { role: "coach", text: snap.answer }];
        waiting = false;
      } else if (snap.phase === "error") {
        messages = [...messages, { role: "coach", text: snap.error ?? "Something went wrong." }];
        waiting = false;
      }
    });
    loadHistory();
    return unsubscribe;
  });

  function send() {
    const q = question.trim();
    if (!q || waiting) return;
    messages = [...messages, { role: "you", text: q }];
    question = "";
    waiting = true;
    stream.ask(q, { sessionId, lapId });
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
  <div class="chat-scroll">
    <div class="thread">
      {#each messages as m, i (i)}
        <div class="msg {m.role}">
          <div class="who">{m.role === "you" ? "YOU" : "⚑"}</div>
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
        placeholder="Ask your engineer…  (e.g. where am I losing time?)"
        bind:value={question}
        oninput={autoGrow}
        onkeydown={onKeydown}
      ></textarea>
      <button type="button" class="send" onclick={send} disabled={waiting}>Send</button>
    </div>
    <div class="hint">{lapId ? `Lap ${lapId} selected` : "Ask about your last lap or overall pace"}</div>
  </div>
</section>
