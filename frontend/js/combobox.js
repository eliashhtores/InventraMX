/* Bounded, on-demand API lookups backed by a select for form serialization. */
;(function (global) {
    "use strict"

    var nextId = 0
    var LIMIT = 20

    global.LookupCombobox = function (select, config) {
        var id = "lookup-" + (++nextId)
        var label = select.closest("label")
        var wrapper = document.createElement("span")
        wrapper.className = "lookup"
        var input = document.createElement("input")
        input.id = id
        input.type = "text"
        input.className = "input"
        input.autocomplete = "off"
        input.placeholder = select.dataset.empty || "Buscar y seleccionar…"
        input.required = select.required
        input.disabled = select.disabled
        input.setAttribute("role", "combobox")
        input.setAttribute("aria-autocomplete", "list")
        input.setAttribute("aria-expanded", "false")
        input.setAttribute("aria-controls", id + "-list")
        input.setAttribute("aria-describedby", id + "-status")
        input.setAttribute("aria-label", label ? label.textContent.trim() : "Almacén")
        if (label) label.htmlFor = id
        var popup = document.createElement("span")
        popup.className = "lookup-popup hidden"
        var list = document.createElement("span")
        list.id = id + "-list"
        list.setAttribute("role", "listbox")
        list.setAttribute("aria-label", input.getAttribute("aria-label"))
        var status = document.createElement("span")
        status.id = id + "-status"
        status.className = "lookup-status"
        status.setAttribute("role", "status")
        popup.append(list, status)
        wrapper.append(input, popup)
        select.after(wrapper)
        select.classList.add("hidden")
        select.setAttribute("aria-hidden", "true")
        select.tabIndex = -1
        select.required = false
        select.replaceChildren(new Option("", ""))

        var selected = null
        var items = []
        var active = -1
        var revision = 0
        var timer

        function close() {
            clearTimeout(timer)
            revision++
            popup.classList.add("hidden")
            input.setAttribute("aria-expanded", "false")
            input.removeAttribute("aria-activedescendant")
            input.removeAttribute("aria-busy")
            active = -1
        }

        function validity() {
            input.setCustomValidity(!selected && input.value ? "Selecciona un resultado de la lista." : "")
        }

        function setItem(item, notify) {
            close()
            selected = item
            select.replaceChildren(new Option("", ""))
            if (item) select.add(new Option(config.label(item), String(item.id)))
            select.value = item ? String(item.id) : ""
            input.value = item ? config.label(item) : ""
            validity()
            if (notify !== false) select.dispatchEvent(new Event("change", { bubbles: true }))
        }

        function setActive(index) {
            active = index
            Array.from(list.children).forEach(function (option, i) {
                option.setAttribute("aria-selected", String(i === active))
            })
            if (active >= 0) {
                input.setAttribute("aria-activedescendant", list.children[active].id)
                list.children[active].scrollIntoView({ block: "nearest" })
            }
        }

        function search() {
            if (input.disabled || document.activeElement !== input) return
            var requestRevision = ++revision
            var params = Object.assign({}, config.params(), {
                search: selected ? "" : input.value.trim(), limit: LIMIT, offset: 0,
            })
            list.replaceChildren()
            items = []
            active = -1
            input.removeAttribute("aria-activedescendant")
            popup.classList.remove("hidden")
            input.setAttribute("aria-expanded", "true")
            input.setAttribute("aria-busy", "true")
            status.textContent = "Buscando…"
            global.Api.get(config.path, params).then(function (page) {
                if (revision !== requestRevision) return
                items = page.results
                items.forEach(function (item, index) {
                    var option = document.createElement("span")
                    option.id = id + "-option-" + index
                    option.className = "lookup-option"
                    option.setAttribute("role", "option")
                    option.setAttribute("aria-selected", "false")
                    option.textContent = config.label(item)
                    option.addEventListener("pointerdown", function (event) { event.preventDefault() })
                    option.addEventListener("click", function (event) {
                        event.preventDefault()
                        setItem(item)
                    })
                    list.appendChild(option)
                })
                status.textContent = page.count > items.length
                    ? "Se muestran " + items.length + " de " + page.count + ". Afina la búsqueda."
                    : items.length ? items.length + " resultado(s)." : "No hay resultados."
            }).catch(function (error) {
                if (revision !== requestRevision) return
                status.textContent = "No se pudo cargar la lista. Vuelve a buscar para reintentar."
                config.onError(error)
            }).finally(function () {
                if (revision === requestRevision) input.removeAttribute("aria-busy")
            })
        }

        input.addEventListener("focus", search)
        input.addEventListener("click", function () {
            if (popup.classList.contains("hidden")) search()
        })
        input.addEventListener("input", function () {
            var hadSelection = selected !== null
            close()
            selected = null
            select.replaceChildren(new Option("", ""))
            validity()
            if (hadSelection) select.dispatchEvent(new Event("change", { bubbles: true }))
            timer = setTimeout(search, 300)
        })
        input.addEventListener("blur", function () {
            close()
            input.removeAttribute("aria-busy")
        })
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                close()
                event.preventDefault()
            } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault()
                if (popup.classList.contains("hidden")) search()
                else if (items.length) setActive(active < 0
                    ? (event.key === "ArrowDown" ? 0 : items.length - 1)
                    : (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length)
            } else if (event.key === "Enter" && !popup.classList.contains("hidden")) {
                event.preventDefault()
                if (active >= 0) setItem(items[active])
            }
        })
        if (select.form) select.form.addEventListener("reset", function () { setItem(null) })

        return {
            select: select,
            input: input,
            getItem: function () { return selected },
            setItem: setItem,
            invalidate: function () {
                close()
                list.replaceChildren()
                input.removeAttribute("aria-busy")
            },
            setDisabled: function (disabled) {
                close()
                select.disabled = disabled
                input.disabled = disabled
                input.removeAttribute("aria-busy")
            },
            setRequired: function (required) { input.required = required },
        }
    }
})(window)
