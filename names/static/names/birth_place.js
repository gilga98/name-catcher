// The place of birth search-select. It always shows where things stand: searching, nothing found, the search being down,
// or the place chosen with its time zone. The form can only send a place chosen from the list.
(function () {
  const MIN_LETTERS = 2;
  const WAIT_MS = 250;
  const NOT_CHOSEN = "Choose your place of birth from the list.";
  const DOWN = "Place search isn't working. Try again in a moment.";

  function csrfToken() {
    return JSON.parse(document.body.getAttribute("hx-headers"))["X-CSRFToken"];
  }

  function setUp(widget) {
    const input = widget.querySelector("[role=combobox]");
    const value = widget.querySelector("input[name=place]");
    const list = widget.querySelector("[role=listbox]");
    const status = widget.querySelector("[data-place-status]");
    let options = [];
    let active = -1;
    let timer;
    let request;

    function say(message, { chosen = false, quiet = false } = {}) {
      status.textContent = message;
      status.classList.toggle("is-chosen", chosen);
      status.classList.toggle("visually-hidden", quiet); // Screen readers need it. Everyone else can see the list.
    }

    function close() {
      list.hidden = true;
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function highlight(index) {
      active = index;
      [...list.children].forEach((row, position) => {
        row.setAttribute("aria-selected", String(position === index));
        if (position === index) {
          input.setAttribute("aria-activedescendant", row.id);
          row.scrollIntoView({ block: "nearest" });
        }
      });
    }

    function row(option, index) {
      const item = document.createElement("li");
      item.id = `${list.id}-${index}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", "false");
      const name = document.createElement("span");
      name.className = "place-name";
      name.textContent = option.name;
      const detail = document.createElement("span");
      detail.className = "place-detail";
      detail.textContent = option.detail;
      item.append(name, detail);
      return item;
    }

    function show(found, typed) {
      options = found;
      list.replaceChildren(...found.map(row));
      if (!found.length) {
        close();
        say(`Nothing found for “${typed}”. Try the nearest larger town.`);
        return;
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      say(`${found.length} ${found.length === 1 ? "place" : "places"} found.`, { quiet: true });
    }

    function choose(option) {
      value.value = option.value;
      input.value = [option.name, option.detail].filter(Boolean).join(", ");
      input.setCustomValidity("");
      close();
      say(`Time zone ${option.timezone}`, { chosen: true });
    }

    async function find(typed) {
      request?.abort();
      request = new AbortController();
      say("Searching…");
      try {
        const response = await fetch(widget.dataset.url, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ q: typed }),
          signal: request.signal,
        });
        if (!response.ok) throw new Error("Search failed");
        show((await response.json()).places, typed);
      } catch (error) {
        if (error.name === "AbortError") return;
        options = [];
        close();
        say(DOWN);
      }
    }

    input.addEventListener("input", () => {
      value.value = ""; // Whatever was chosen no longer matches what's in the box, so nothing may still vouch for it.
      say("");
      clearTimeout(timer);
      request?.abort();
      const typed = input.value.trim();
      input.setCustomValidity(typed ? NOT_CHOSEN : "");
      if (typed.length < MIN_LETTERS) {
        options = [];
        close();
        return;
      }
      timer = setTimeout(() => find(typed), WAIT_MS);
    });

    input.addEventListener("keydown", (event) => {
      const open = !list.hidden;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!open && options.length) show(options, input.value);
        if (options.length) {
          const step = event.key === "ArrowDown" ? 1 : -1;
          highlight((active + step + options.length) % options.length);
        }
      } else if (event.key === "Enter" && !value.value) {
        event.preventDefault(); // A half-typed place must not send the form.
        if (open && active >= 0) choose(options[active]);
        else if (open) highlight(0);
        else if (input.value.trim()) input.reportValidity();
      } else if (event.key === "Escape" && open) {
        event.preventDefault();
        close();
      }
    });

    // Fires before the box loses focus, so a tap on a row isn't lost to the list closing.
    list.addEventListener("mousedown", (event) => event.preventDefault());
    list.addEventListener("click", (event) => {
      const item = event.target.closest("[role=option]");
      if (item) choose(options[[...list.children].indexOf(item)]);
    });
    input.addEventListener("blur", close);
    input.addEventListener("focus", () => {
      if (value.value) input.select(); // Changing a chosen place means replacing it, so one keystroke starts over.
      else if (options.length) show(options, input.value);
    });

    if (input.value && !value.value) input.setCustomValidity(NOT_CHOSEN);
  }

  htmx.onLoad((content) => {
    const widgets = content.matches?.("[data-place-search]") ? [content] : content.querySelectorAll("[data-place-search]");
    widgets.forEach(setUp);
  });
})();
