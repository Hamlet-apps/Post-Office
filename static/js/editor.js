/* Post Office: the message editor (campaigns and templates). No build step.

   The page is rendered by content/editor.py for one language, one form (email or
   notification) and one view (visual or HTML). This script keeps the fields in an
   Alpine component, saves every change after one second, and swaps in the email
   canvas the server renders with content.render, so what you see is what is sent. */
(function () {
  "use strict";

  var EMPTY = { id: "", type: "" };

  function readState() {
    var el = document.getElementById("editor-state");
    try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; }
  }

  function newId() {
    return "b" + Date.now().toString(36) + Math.floor(Math.random() * 46656).toString(36);
  }

  function toast(text, isError) {
    if (window.poToast) window.poToast(text, isError);
  }

  function editor() {
    var s = readState();
    var urls = s.urls || {};
    var timer = null;        // the autosave debounce
    var seq = 0;             // the newest save wins
    var sortable = null;
    var lastField = null;    // the text field placeholders are inserted into
    var dragging = false;
    var htmlDirty = false;

    return {
      i18n: s.i18n || {},
      editable: !!s.editable,
      kind: s.kind,
      lang: s.lang,
      form: s.form,
      view: s.view,
      storedMode: s.mode || "visual",
      sample: false,
      width: "desktop",
      langs: s.langs || [],
      name: s.name || "",
      subject: s.subject || "",
      previewText: s.preview_text || "",
      blocks: s.blocks || [],
      html: s.html || "",
      text: s.text || "",
      buttonLabel: s.button_label || "",
      buttonUrl: s.button_url || "",
      imageUrl: s.image_url || "",
      note: s.note || { telegram_html: "", push_body: "", button_label: "", button_url: "", image_url: "", fallback: false },
      blank: !!s.blank,
      customised: !!s.customised,
      problems: s.problems || {},
      warnings: s.warnings || [],
      doc: s.doc || "",        // content written as HTML is previewed in a sandboxed frame
      error: "",
      sel: "",
      status: "idle",

      init: function () {
        var self = this;
        this.decorate();
        if (this.editable && this.$refs.palette && window.Sortable) {
          window.Sortable.create(this.$refs.palette, {
            group: { name: "blocks", pull: "clone", put: false },
            sort: false,
            draggable: ".ed-pal",
            filter: "button",
            preventOnFilter: false,
            onStart: function () { dragging = true; },
            onEnd: function () { dragging = false; }
          });
        }
        // Leaving with a change still waiting: send it on the way out.
        window.addEventListener("pagehide", function () {
          if (self.status === "dirty") self.send(self.payload(), true);
        });
      },

      // ------------------------------------------------------------ small readers

      get selected() {
        var id = this.sel;
        for (var i = 0; i < this.blocks.length; i++) if (this.blocks[i].id === id) return this.blocks[i];
        return null;
      },
      // Never null, so the panel's bindings cannot fail while it closes.
      get cur() { return this.selected || EMPTY; },
      get selectedIndex() {
        for (var i = 0; i < this.blocks.length; i++) if (this.blocks[i].id === this.sel) return i;
        return -1;
      },
      get limit() {
        var limits = s.limits || { text: 4096, caption: 1024 };
        return this.imageUrl ? limits.caption : limits.text;
      },
      get count() { return this.text.length; },
      get countText() { return this.count.toLocaleString() + " / " + this.limit.toLocaleString(); },
      get problemCount() { return Object.keys(this.problems || {}).length; },
      get writtenText() {
        var n = this.langs.filter(function (l) { return l.written; }).length;
        return (this.i18n.written_of || "").replace("{n}", n).replace("{total}", this.langs.length);
      },
      get statusText() {
        if (this.status === "saving") return this.i18n.saving;
        if (this.status === "dirty") return this.i18n.unsaved;
        if (this.status === "error") return this.i18n.failed;
        if (this.status === "saved") return this.i18n.saved;
        return "";
      },
      langNote: function (l) {
        if (!l.written) return this.i18n.not_written;
        return l.is_default ? this.i18n.default : "";
      },
      typeLabel: function (type) { return (this.i18n.types || {})[type] || type; },
      pageUrl: function (changes) {
        var p = { lang: this.lang, form: this.form };
        if (this.form === "email" && this.view === "html") p.view = "html";
        Object.keys(changes || {}).forEach(function (k) { p[k] = changes[k]; });
        return urls.page + "?" + new URLSearchParams(p).toString();
      },
      listFields: function (source) {
        var found = (s.placeholders || []).filter(function (p) { return p.name === source && p.list; })[0];
        return found ? found.fields : [];
      },

      // ------------------------------------------------------------ saving

      payload: function (extra) {
        var p = { lang: this.lang, form: this.form, sample: this.sample };
        if (this.kind === "campaign" || !s.allow_repeat) p.name = this.name;
        if (this.form === "email") {
          p.subject = this.subject;
          p.preview_text = this.previewText;
          if (this.view === "html") {
            if (htmlDirty || this.storedMode === "html") p.html = this.html;
          } else {
            p.blocks = this.blocks;
          }
        } else {
          p.text = this.text;
          p.button_label = this.buttonLabel;
          p.button_url = this.buttonUrl;
        }
        Object.keys(extra || {}).forEach(function (k) { p[k] = extra[k]; });
        return p;
      },

      send: function (body, leaving) {
        return fetch(urls.save, {
          method: "POST",
          credentials: "same-origin",
          keepalive: !!leaving,
          headers: { "Content-Type": "application/json", "X-CSRFToken": s.csrf },
          body: JSON.stringify(body)
        });
      },

      // Every change calls this. Text waits a second; structure is saved at once.
      touch: function (now) {
        var self = this;
        if (!this.editable) return;
        this.status = "dirty";
        clearTimeout(timer);
        timer = setTimeout(function () { self.save(); }, now ? 0 : 1000);
      },

      save: function (extra) {
        var self = this;
        if (!this.editable) return Promise.resolve(false);
        clearTimeout(timer);
        var mine = ++seq;
        this.status = "saving";
        return this.send(this.payload(extra))
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (mine !== seq) return true;            // a newer save is on its way
            if (!data.ok) {
              self.status = "error";
              self.error = data.error || self.i18n.failed;
              if (data.field !== "html") toast(self.error, true);
              return false;
            }
            self.error = "";
            self.apply(data);
            if (self.status === "saving") self.status = "saved";
            return true;
          })
          .catch(function () {
            if (mine === seq) self.status = "error";
            return false;
          });
      },

      // What the server sends back after a save: tab states, canvas, previews.
      apply: function (data) {
        if (data.langs) this.langs = data.langs;
        this.problems = data.problems || {};
        this.warnings = data.warnings || [];
        this.customised = !!data.customised;
        if (data.mode) this.storedMode = data.mode;
        if (data.note) this.note = data.note;
        if (typeof data.image_url === "string") this.imageUrl = data.image_url;
        if (typeof data.doc === "string") this.doc = data.doc;
        if (typeof data.canvas === "string" && !dragging) this.setCanvas(data.canvas);
      },

      // The preview frame grows to fit the email, so the page scrolls, not the frame.
      fitFrame: function () {
        var frame = this.$refs.frame;
        try {
          if (frame && frame.contentDocument) frame.style.height = frame.contentDocument.documentElement.scrollHeight + "px";
        } catch (e) { /* leave the default height */ }
      },

      refresh: function () {
        var self = this;
        var q = new URLSearchParams({ lang: this.lang, form: this.form, sample: this.sample ? "1" : "" });
        return fetch(urls.canvas + "?" + q.toString(), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (data) { if (data.ok) self.apply(data); })
          .catch(function () {});
      },

      // Save anything pending, then follow a link (language, form, next step).
      go: function (url) {
        if (!this.editable || this.status !== "dirty") { window.location.href = url; return; }
        this.save().then(function (ok) {
          if (ok) window.location.href = url;
        });
      },

      setView: function (view) {
        var self = this;
        if (view === this.view) return;
        if (view === "html") { this.go(this.pageUrl({ view: "html" })); return; }
        var url = this.pageUrl({ view: "visual" });
        if (this.storedMode !== "html" && !htmlDirty) { this.go(url); return; }
        if (!this.editable) return;
        if (!window.confirm(this.i18n.back_to_blocks)) return;
        htmlDirty = false;
        this.view = "visual";       // so the payload carries no HTML
        this.save({ mode: "visual", blocks: this.blocks }).then(function (ok) {
          if (ok) window.location.href = url; else self.view = "html";
        });
      },

      htmlTouched: function () {
        htmlDirty = true;
        this.touch();
      },

      setSample: function (on) {
        var self = this;
        this.sample = !!on;
        if (this.editable && this.status === "dirty") this.save(); else self.refresh();
      },

      // ------------------------------------------------------------ the canvas

      setCanvas: function (html) {
        var box = this.$refs.canvas;
        if (!box) return;
        var y = window.scrollY;
        box.innerHTML = html;
        this.decorate();
        window.scrollTo(window.scrollX, y);
      },

      // Add handles, the drop zone and the footer note to the server-rendered email.
      decorate: function () {
        var self = this, box = this.$refs.canvas;
        if (sortable) { sortable.destroy(); sortable = null; }
        if (!box) return;
        var visual = this.view === "visual";
        var types = {};
        this.blocks.forEach(function (b) { types[b.id] = b.type; });

        box.querySelectorAll("td[data-block-id]").forEach(function (td) {
          var id = td.getAttribute("data-block-id"), type = types[id] || "";
          if (!td.firstChild) {
            var stub = document.createElement("div");
            stub.className = "ed-stub";
            stub.textContent = (self.i18n.stubs || {})[type] || self.typeLabel(type);
            td.appendChild(stub);
          }
          if (!visual) return;
          td.parentNode.classList.add("ed-block");
          td.setAttribute("tabindex", "0");
          td.setAttribute("role", "button");
          td.setAttribute("aria-label", self.typeLabel(type) + " " + (self.i18n.block || ""));
          if (self.editable && self.$refs.handleTpl) {
            td.insertBefore(self.$refs.handleTpl.content.firstElementChild.cloneNode(true), td.firstChild);
          }
        });

        var footer = box.querySelector("td[data-footer]");
        if (footer && this.$refs.lockTpl) {
          var note = this.$refs.lockTpl.content.firstElementChild.cloneNode(true);
          note.querySelector("span").textContent = this.i18n.footer_note || "";
          footer.insertBefore(note, footer.firstChild);
        }

        var body = box.querySelector("tbody[data-blocks]");
        if (!visual || !body) return;
        this.markSelected();
        if (!this.editable) return;

        var drop = document.createElement("tr");
        drop.className = "ed-drop";
        var cell = document.createElement("td");
        var inner = document.createElement("div");
        inner.textContent = this.i18n.drop_here || "";
        cell.appendChild(inner);
        drop.appendChild(cell);
        body.appendChild(drop);

        if (!window.Sortable) return;
        sortable = window.Sortable.create(body, {
          group: { name: "blocks", put: true },
          draggable: "tr.ed-block",
          handle: ".ed-handle",
          animation: 150,
          ghostClass: "ed-ghost",
          onStart: function () { dragging = true; },
          onEnd: function (evt) {
            dragging = false;
            if (evt.to === evt.from) self.reordered();
          },
          // A block dragged in from the palette.
          onAdd: function (evt) {
            dragging = false;
            var type = evt.item.getAttribute("data-type"), index = 0, prev = evt.item.previousElementSibling;
            while (prev) {
              if (prev.classList.contains("ed-block")) index++;
              prev = prev.previousElementSibling;
            }
            if (evt.item.parentNode) evt.item.parentNode.removeChild(evt.item);
            if (type) self.addBlock(type, index);
          }
        });
      },

      markSelected: function () {
        var box = this.$refs.canvas, id = this.sel;
        if (!box) return;
        box.querySelectorAll("td[data-block-id]").forEach(function (td) {
          var on = td.getAttribute("data-block-id") === id;
          td.classList.toggle("is-selected", on);
          td.setAttribute("aria-pressed", on ? "true" : "false");
        });
      },

      select: function (id) {
        this.sel = id || "";
        lastField = null;
        this.markSelected();
      },

      canvasClick: function (evt) {
        if (evt.target.closest("a")) evt.preventDefault();   // links in the preview do not navigate
        if (this.view !== "visual") return;
        var td = evt.target.closest("td[data-block-id]");
        if (td) this.select(td.getAttribute("data-block-id"));
      },

      canvasKey: function (evt) {
        if (this.view !== "visual" || (evt.key !== "Enter" && evt.key !== " ")) return;
        var td = evt.target.closest("td[data-block-id]");
        if (!td || evt.target !== td) return;
        evt.preventDefault();
        this.select(td.getAttribute("data-block-id"));
        var root = this.$root;
        this.$nextTick(function () {
          var field = root.querySelector(".ed-panel [data-main]");
          if (field) field.focus();
        });
      },

      // The new order is whatever the rows say after a drop.
      reordered: function () {
        var box = this.$refs.canvas, byId = {}, next = [];
        this.blocks.forEach(function (b) { byId[b.id] = b; });
        box.querySelectorAll("tbody[data-blocks] td[data-block-id]").forEach(function (td) {
          var b = byId[td.getAttribute("data-block-id")];
          if (b) { next.push(b); delete byId[b.id]; }
        });
        Object.keys(byId).forEach(function (id) { next.push(byId[id]); });
        this.blocks = next;
        this.touch(true);
      },

      // ------------------------------------------------------------ blocks

      makeBlock: function (type, extra) {
        var d = this.i18n.defaults || {};
        var fields = {
          heading: { text: d.heading || "", align: "left" },
          text: { text: d.text || "" },
          image: { src: "", alt: "", href: null },
          button: { text: d.button || "", href: "" },
          card: { title: d.card_title || "", text: d.card_text || "" },
          divider: {},
          repeat: { source: "", show: [] }
        }[type];
        if (!fields) return null;
        var block = { id: newId(), type: type };
        Object.keys(fields).forEach(function (k) { block[k] = fields[k]; });
        Object.keys(extra || {}).forEach(function (k) { block[k] = extra[k]; });
        return block;
      },

      addBlock: function (type, index, extra) {
        if (!this.editable) return;
        var block = this.makeBlock(type, extra);
        if (!block) return;
        var at = (typeof index === "number" && index >= 0 && index <= this.blocks.length) ? index : this.blocks.length;
        this.blocks.splice(at, 0, block);
        this.sel = block.id;
        lastField = null;
        this.touch(true);
      },

      addRepeat: function (source) {
        var fields = this.listFields(source).map(function (f) { return f.name; });
        this.addBlock("repeat", undefined, { source: source, show: fields });
      },

      paletteClick: function (evt) {
        var btn = evt.target.closest("[data-add]");
        if (btn) this.addBlock(btn.getAttribute("data-add"));
      },

      duplicate: function () {
        var at = this.selectedIndex;
        if (at < 0 || !this.editable) return;
        var copy = JSON.parse(JSON.stringify(this.blocks[at]));
        copy.id = newId();
        this.blocks.splice(at + 1, 0, copy);
        this.sel = copy.id;
        this.touch(true);
      },

      remove: function () {
        var at = this.selectedIndex;
        if (at < 0 || !this.editable) return;
        this.blocks.splice(at, 1);
        this.sel = "";
        lastField = null;
        this.touch(true);
      },

      move: function (delta) {
        var at = this.selectedIndex, to = at + delta;
        if (at < 0 || to < 0 || to >= this.blocks.length || !this.editable) return;
        var block = this.blocks.splice(at, 1)[0];
        this.blocks.splice(to, 0, block);
        this.touch(true);
      },

      setField: function (name, value) {
        if (!this.selected || !this.editable) return;
        this.selected[name] = value;
        this.touch(true);
      },

      sourceChanged: function () {
        if (!this.selected) return;
        this.selected.show = this.listFields(this.selected.source).map(function (f) { return f.name; });
        this.touch(true);
      },

      toggleShow: function (field) {
        var block = this.selected;
        if (!block || !this.editable) return;
        var show = block.show || [], on = show.indexOf(field) === -1;
        // Keep the building's own order of fields.
        block.show = this.listFields(block.source).map(function (f) { return f.name; }).filter(function (n) {
          return n === field ? on : show.indexOf(n) !== -1;
        });
        this.touch(true);
      },

      // ------------------------------------------------------------ placeholders and marks

      remember: function (evt) {
        if (evt.target && evt.target.matches && evt.target.matches("[data-insertable]")) lastField = evt.target;
      },

      // With "after", wraps the selection (or a stand-in word); without, inserts at the cursor.
      insertText: function (el, before, after, fallback) {
        var value = el.value || "";
        var start = typeof el.selectionStart === "number" ? el.selectionStart : value.length;
        var end = typeof el.selectionEnd === "number" ? el.selectionEnd : value.length;
        var wrapping = after !== undefined;
        var middle = wrapping ? (value.slice(start, end) || fallback || "") : "";
        el.value = value.slice(0, start) + before + middle + (wrapping ? after : "") + value.slice(end);
        el.dispatchEvent(new Event("input", { bubbles: true }));   // x-model and autosave follow
        el.focus();
        var from = start + before.length, to = from + middle.length;
        try { el.setSelectionRange(from, to); } catch (e) { /* not a text field */ }
      },

      insertVar: function (name) {
        if (!this.editable) return;
        var el = lastField;
        if (!el || !document.body.contains(el)) el = this.$root.querySelector("[data-insertable][data-main]");
        if (!el) { toast(this.i18n.pick_field); return; }
        var token = "{" + name + "}";
        if (el.hasAttribute("data-jinja")) {
          var path = (name === "first_name" || name === "last_name") ? "person." + name : name;
          token = "{{ " + path + " }}";
        }
        this.insertText(el, token);
      },

      // The notification toolbar: wrap the selection in *bold*, _italic_ or [label](url).
      wrap: function (before, after, fallback) {
        if (!this.editable || !this.$refs.note) return;
        this.insertText(this.$refs.note, before, after, fallback);
      },

      // ------------------------------------------------------------ pictures

      postFile: function (url, file, fields) {
        var body = new FormData();
        if (file) body.append("file", file);
        Object.keys(fields || {}).forEach(function (k) { body.append(k, fields[k]); });
        return fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": s.csrf }, body: body })
          .then(function (r) { return r.json(); });
      },

      uploadImage: function (evt) {
        var self = this, input = evt.target, file = input.files && input.files[0], block = this.selected;
        if (!file || !block) return;
        this.postFile(urls.upload, file).then(function (data) {
          input.value = "";
          if (!data.ok) { toast(data.error || self.i18n.upload_failed, true); return; }
          block.src = data.url;
          self.touch(true);
        }).catch(function () { toast(self.i18n.upload_failed, true); });
      },

      uploadNoteImage: function (evt) {
        var self = this, input = evt.target, file = input.files && input.files[0];
        if (!file) return;
        // Store the text first, so the row the picture is attached to is current.
        this.save().then(function () {
          return self.postFile(urls.image, file, { lang: self.lang });
        }).then(function (data) {
          input.value = "";
          if (!data.ok) { toast(data.error || self.i18n.upload_failed, true); return; }
          self.apply(data);
          self.status = "saved";
        }).catch(function () { toast(self.i18n.upload_failed, true); });
      },

      removeNoteImage: function () {
        var self = this;
        this.postFile(urls.image, null, { lang: this.lang, remove: "1" }).then(function (data) {
          if (data.ok) { self.apply(data); self.status = "saved"; }
        }).catch(function () { toast(self.i18n.failed, true); });
      },

      // ------------------------------------------------------------ an empty language

      copyFromDefault: function () {
        var self = this;
        fetch(urls.copy, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": s.csrf },
          body: JSON.stringify({ lang: this.lang, form: this.form })
        }).then(function (r) { return r.json(); }).then(function (data) {
          if (!data.ok) { toast(data.error || self.i18n.failed, true); return; }
          window.location.href = self.pageUrl({});
        }).catch(function () { toast(self.i18n.failed, true); });
      },

      startBlank: function () {
        this.blank = false;
        if (this.form === "email" && this.view === "visual" && !this.blocks.length) this.addBlock("heading");
      }
    };
  }

  document.addEventListener("alpine:init", function () {
    window.Alpine.data("poEditor", editor);
  });
})();
