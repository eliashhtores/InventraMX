/* InventraMX single-page frontend. All data is loaded from the REST API via XHR (see api.js). */
;(function () {
    "use strict"

    var VIEWS = ["dashboard", "products", "lots", "warehouses", "locations", "stock", "movements", "reservations"]
    var PAGE_SIZE = 50
    var lookupControls = new Map()
    var offsets = {}

    // ---------- DOM helpers ----------

    function $(selector, root) {
        return (root || document).querySelector(selector)
    }
    function $all(selector, root) {
        return Array.prototype.slice.call((root || document).querySelectorAll(selector))
    }

    /** Create an element; text content is always set via textContent to avoid HTML injection. */
    function el(tag, attrs, children) {
        var node = document.createElement(tag)
        Object.keys(attrs || {}).forEach(function (key) {
            if (key === "text") node.textContent = attrs[key]
            else if (key === "className") node.className = attrs[key]
            else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), attrs[key])
            else node.setAttribute(key, attrs[key])
        })
        ;(children || []).forEach(function (child) {
            node.appendChild(typeof child === "string" ? document.createTextNode(child) : child)
        })
        return node
    }

    function td(value, className) {
        return el("td", { text: value === null || value === undefined ? "" : String(value), className: className || "" })
    }

    function renderRows(tbody, items, rowFn, emptyText) {
        tbody.replaceChildren()
        if (!items.length) {
            var colspan = tbody.closest("table").querySelectorAll("thead th").length
            tbody.appendChild(
                el("tr", {}, [el("td", { colspan: colspan, className: "text-center text-slate-400 py-6", text: emptyText || "No hay registros." })]),
            )
            return
        }
        items.forEach(function (item) {
            tbody.appendChild(rowFn(item))
        })
    }

    var toastTimer
    var announcementTimer
    function toast(message, isError) {
        var box = $("#toast")
        box.textContent = message
        box.className = "fixed top-4 right-4 z-50 px-4 py-2 rounded shadow text-sm text-white " + (isError ? "bg-red-600" : "bg-emerald-600")
        var status = $("#sr-status")
        var alert = $("#sr-alert")
        status.textContent = ""
        alert.textContent = ""
        clearTimeout(announcementTimer)
        announcementTimer = setTimeout(function () {
            ;(isError ? alert : status).textContent = message
        }, 50)
        clearTimeout(toastTimer)
        toastTimer = setTimeout(
            function () {
                box.classList.add("hidden")
            },
            isError ? 6000 : 3000,
        )
        box.classList.remove("hidden")
    }

    function showError(err) {
        toast(err.message || String(err), true)
    }

    function formToJson(form) {
        var data = {}
        new FormData(form).forEach(function (value, key) {
            var input = form.elements[key]
            if (input && input.type === "datetime-local") {
                if (value !== "") data[key] = new Date(value).toISOString()
                return
            }
            if (input && input.type === "date") {
                data[key] = value || null
                return
            }
            if (input && input.dataset.type === "boolean") {
                data[key] = value === "true"
                return
            }
            if (value === "") {
                data[key] = input && input.tagName === "SELECT" ? null : value
                return
            }
            data[key] = input && input.type === "number" ? Number(value) : value
        })
        $all('input[type="checkbox"]', form).forEach(function (input) {
            data[input.name] = input.checked
        })
        return data
    }

    function formatDate(iso) {
        return iso ? new Date(iso).toLocaleString("es-MX") : ""
    }

    function debounce(fn, wait) {
        var timer
        return function () {
            var args = arguments
            clearTimeout(timer)
            timer = setTimeout(function () {
                fn.apply(null, args)
            }, wait)
        }
    }

    function results(page) {
        return page && page.results ? page.results : page || []
    }

    // ---------- Pagination ----------

    /** Fetch one page of a list endpoint for a view, using that view's current offset. */
    function fetchPage(view, path, params) {
        params = params || {}
        params.limit = PAGE_SIZE
        params.offset = offsets[view] || 0
        return Api.get(path, params).then(function (page) {
            // If records were removed and the current page is now past the end, step back.
            if (!page.results.length && params.offset > 0 && page.count > 0) {
                offsets[view] = Math.max(0, Math.floor((page.count - 1) / PAGE_SIZE) * PAGE_SIZE)
                return fetchPage(view, path, params)
            }
            return page
        })
    }

    /** Fetch every page only for explicit filtered exports. */
    function fetchAll(path, params, acc) {
        acc = acc || []
        var query = Object.assign({}, params, { limit: 1000, offset: acc.length })
        return Api.get(path, query).then(function (page) {
            acc = acc.concat(page.results)
            if (page.next && !page.results.length) throw new Error("La API devolvió una página vacía con más resultados pendientes.")
            return page.next ? fetchAll(path, params, acc) : acc
        })
    }

    function stockFilters() {
        return { warehouse: $("#stock-warehouse").value, search: $("#stock-search").value.trim() }
    }

    function movementFilters() {
        var from = $("#movement-date-from")
        var to = $("#movement-date-to")
        if (!from.validity.valid || !to.validity.valid) throw new Error("Introduce fechas válidas.")
        if (from.value && to.value && from.value > to.value) {
            throw new Error("La fecha hasta debe ser igual o posterior a la fecha desde.")
        }
        var params = { movement_type: $("#movement-filter").value, search: $("#movement-search").value.trim() }
        if (from.value) params.occurred_at_after = new Date(from.value + "T00:00:00").toISOString()
        if (to.value) {
            var end = new Date(to.value + "T00:00:00")
            end.setDate(end.getDate() + 1)
            // Include the full local day, down to PostgreSQL's microsecond precision.
            params.occurred_at_before = new Date(end.getTime() - 1).toISOString().replace(".999Z", ".999999Z")
        }
        return params
    }

    function csvCell(value) {
        var text = value === null || value === undefined ? "" : String(value)
        // Quoting alone does not stop spreadsheets from interpreting formulas.
        if (typeof value === "string" && /^[\s\u0000-\u001f]*[=+\-@]/.test(text)) text = "'" + text
        return '"' + text.replace(/"/g, '""') + '"'
    }

    function bindCsvExport(buttonId, path, getFilters, tbodyId, filename, rowValues) {
        var button = $(buttonId)
        button.addEventListener("click", function () {
            if (button.disabled) return
            var label = button.textContent
            button.disabled = true
            button.textContent = "Exportando…"
            Promise.resolve().then(function () {
                return fetchAll(path, getFilters())
            }).then(function (rows) {
                var headers = $all("thead th", $(tbodyId).closest("table")).map(function (th) { return th.textContent.trim() })
                var csv = [headers].concat(rows.map(rowValues)).map(function (row) {
                    return row.map(csvCell).join(",")
                }).join("\r\n") + "\r\n"
                var url = URL.createObjectURL(new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }))
                var link = el("a", { href: url, download: filename })
                document.body.appendChild(link)
                try {
                    link.click()
                } finally {
                    link.remove()
                    setTimeout(function () { URL.revokeObjectURL(url) }, 1000)
                }
                toast("CSV exportado: " + rows.length + " registro(s).")
            }).catch(showError).finally(function () {
                button.disabled = false
                button.textContent = label
            })
        })
    }

    function resetPage(view) {
        offsets[view] = 0
    }

    /** Render "Showing a–b of n" with Previous/Next buttons below the table that owns tbody. */
    function renderPager(tbody, view, page) {
        var table = tbody.closest("table")
        var pager = table.nextElementSibling
        if (!pager || !pager.classList.contains("pager")) {
            pager = el("div", { className: "pager" })
            table.parentNode.insertBefore(pager, table.nextSibling)
        }
        var offset = offsets[view] || 0
        var shown = page.results.length
        var summary = page.count ? "Mostrando " + (offset + 1) + "–" + (offset + shown) + " de " + page.count : ""
        function go(delta) {
            return function () {
                offsets[view] = Math.max(0, offset + delta)
                loadCurrent()
            }
        }
        var prev = el("button", { type: "button", className: "btn-link", text: "‹ Anterior", onclick: go(-PAGE_SIZE) })
        var next = el("button", { type: "button", className: "btn-link", text: "Siguiente ›", onclick: go(PAGE_SIZE) })
        prev.disabled = !page.previous
        next.disabled = !page.next
        pager.replaceChildren(el("span", { text: summary }), el("span", { className: "space-x-3" }, [prev, next]))
        pager.classList.toggle("hidden", !page.count)
    }

    /** Render a page of results into tbody plus its pager. */
    function renderPage(tbody, view, page, rowFn, emptyText) {
        renderRows(tbody, results(page), rowFn, emptyText)
        renderPager(tbody, view, page)
    }

    // ---------- On-demand searchable lookups ----------

    var LOCATION_LABELS = {
        storage: "Almacenamiento",
        receiving: "Recepción",
        picking: "Preparación de pedidos",
        shipping: "Envío",
    }
    var MOVEMENT_LABELS = {
        inbound: "Entrada",
        outbound: "Salida",
        transfer: "Transferencia",
        adjustment: "Ajuste",
    }
    var LABELS = {
        warehouses: function (w) {
            return w.code + (w.name ? " - " + w.name : "")
        },
        activeWarehouses: function (w) {
            return LABELS.warehouses(w)
        },
        locations: function (l) {
            return l.warehouse_code + "/" + l.code + " (" + (LOCATION_LABELS[l.location_type] || l.location_type) + ")"
        },
        products: function (p) {
            return p.sku + (p.name ? " - " + p.name : "")
        },
        trackedProducts: function (p) {
            return LABELS.products(p)
        },
        lots: function (lot) {
            return lot.lot_number + (lot.expiry_date ? " · " + lot.expiry_date : "")
        },
        reservableStock: function (s) {
            return s.product_sku + (s.lot_number ? " [" + s.lot_number + "]" : "") +
                " @ " + s.warehouse_code + "/" + s.location_code + " — " + s.available_quantity + " disponibles"
        },
    }

    function setupLookups() {
        var sources = {
            warehouses: { path: "/warehouses/" },
            activeWarehouses: { path: "/warehouses/", params: { is_active: "true" } },
            locations: { path: "/locations/", params: { is_active: "true", warehouse_is_active: "true" } },
            products: { path: "/products/", params: { is_active: "true" } },
            trackedProducts: { path: "/products/", params: { is_active: "true", lot_tracked: "true" } },
            reservableStock: { path: "/stock/", params: { reservable: "true" } },
            lots: { path: "/lots/" },
        }
        $all("select[data-options]").forEach(function (select) {
            var kind = select.dataset.options
            var source = sources[kind]
            lookupControls.set(select, new LookupCombobox(select, {
                path: source.path,
                label: LABELS[kind],
                params: function () {
                    if (kind === "lots") return { product: $("#movement-form").elements.product.value }
                    return source.params || {}
                },
                onError: showError,
            }))
        })
    }

    function invalidateLookups(path, id) {
        var kinds = {
            "/warehouses/": ["warehouses", "activeWarehouses", "locations", "reservableStock"],
            "/locations/": ["locations", "reservableStock"],
            "/products/": ["products", "trackedProducts", "lots", "reservableStock"],
            "/lots/": ["lots", "reservableStock"],
            "/movements/": ["reservableStock"],
            "/reservations/": ["reservableStock"],
        }[path]
        var relation = {
            "/warehouses/": "warehouse", "/locations/": "location",
            "/products/": "product", "/lots/": "lot",
        }[path]
        lookupControls.forEach(function (control, select) {
            if (kinds.indexOf(select.dataset.options) === -1) return
            control.invalidate()
            var item = control.getItem()
            if (!item) return
            var matches = select.dataset.options === "reservableStock"
                ? (!relation || path === "/warehouses/" || item[relation] === id)
                : (item[relation] || item.id) === id
            if (matches) {
                control.setItem(null, false)
                if (select.id === "location-filter") resetPage("locations")
                if (select.id === "stock-warehouse") resetPage("stock")
            }
        })
        updateMovementLotForm()
    }

    // ---------- Views ----------

    function loadDashboard() {
        return Api.get("/reports/summary/").then(function (data) {
            var cards = [
                ["Almacenes", data.warehouses],
                ["Ubicaciones", data.locations],
                ["Productos activos", data.products],
                ["Unidades en inventario", data.total_units],
            ]
            $("#summary-cards").replaceChildren.apply(
                $("#summary-cards"),
                cards.map(function (c) {
                    return el("div", { className: "card" }, [
                        el("div", { className: "text-xs uppercase text-slate-500", text: c[0] }),
                        el("div", { className: "text-2xl font-semibold mt-1", text: String(c[1]) }),
                    ])
                }),
            )
            var list = $("#summary-movements")
            list.replaceChildren()
            Object.keys(data.movements).forEach(function (type) {
                list.appendChild(
                    el("li", { className: "flex justify-between" }, [
                        el("span", { className: "badge badge-" + type, text: MOVEMENT_LABELS[type] || type }),
                        el("span", { className: "font-medium", text: String(data.movements[type]) }),
                    ]),
                )
            })
            var lowStockSummary = $("#low-stock-summary")
            if (!Number.isInteger(data.low_stock_count) || data.low_stock_count < data.low_stock.length) {
                var countMessage = "El total de alertas globales no está disponible. Actualiza el backend y recarga la página."
                lowStockSummary.textContent = countMessage
                throw new Error(countMessage)
            }
            lowStockSummary.textContent = data.low_stock_count + " productos activos con poco inventario" +
                (data.low_stock.length < data.low_stock_count ? " (se muestran " + data.low_stock.length + ")" : "")
            renderRows(
                $("#low-stock-body"),
                data.low_stock,
                function (p) {
                    return el("tr", {}, [
                        td(p.sku),
                        td(p.name),
                        td(p.total_quantity, "text-right text-red-600 font-medium"),
                        td(p.reorder_level, "text-right"),
                    ])
                },
                "Todos los productos superan su nivel de reposición.",
            )
            var warehouseSummary = $("#warehouse-summary")
            warehouseSummary.replaceChildren()
            if (!Array.isArray(data.warehouse_breakdown)) {
                var message = "El desglose por almacén no está disponible. Actualiza el backend y recarga la página."
                warehouseSummary.appendChild(el("p", { className: "text-sm text-red-600", text: message }))
                throw new Error(message)
            }
            data.warehouse_breakdown.forEach(function (warehouse) {
                var body = el("tbody")
                var table = el("table", { className: "table" }, [
                    el("thead", {}, [
                        el("tr", {}, [
                            el("th", { text: "SKU" }),
                            el("th", { text: "Nombre" }),
                            el("th", { className: "text-right", text: "Existencias" }),
                            el("th", { className: "text-right", text: "Nivel de reposición" }),
                        ]),
                    ]),
                    body,
                ])
                var shown = warehouse.low_stock.length
                warehouseSummary.appendChild(el("section", {}, [
                    el("h3", {
                        className: "font-semibold",
                        text: warehouse.code + " — " + warehouse.name + (warehouse.is_active ? "" : " (inactivo)"),
                    }),
                    el("p", {
                        className: "text-sm text-slate-500",
                        text: warehouse.total_units + " unidades · " + warehouse.low_stock_count + " productos con poco inventario" +
                            (shown < warehouse.low_stock_count ? " (se muestran " + shown + ")" : ""),
                    }),
                    table,
                ]))
                renderRows(body, warehouse.low_stock, function (product) {
                    return el("tr", {}, [
                        td(product.sku), td(product.name),
                        td(product.total_quantity, "text-right text-red-600 font-medium"),
                        td(product.reorder_level, "text-right"),
                    ])
                }, "Todos los productos superan su nivel de reposición.")
            })
            if (!data.warehouse_breakdown.length) {
                warehouseSummary.appendChild(el("p", { className: "text-sm text-slate-500", text: "Aún no hay almacenes." }))
            }
        })
    }

    function toggleActive(path, item) {
        return function () {
            Api.patch(path + item.id + "/", { is_active: !item.is_active })
                .then(function () {
                    toast("Actualizado.")
                    invalidateLookups(path, item.id)
                })
                .then(loadCurrent)
                .catch(showError)
        }
    }

    function deleteRecord(path, item, label) {
        return function () {
            if (!window.confirm("¿Eliminar " + label + "?")) return
            Api.del(path + item.id + "/")
                .then(function () {
                    toast("Eliminado.")
                    invalidateLookups(path, item.id)
                })
                .then(loadCurrent)
                .catch(showError)
        }
    }

    var editHandlers = {}

    function actionCell(path, item, label, onEdit) {
        var actions = []
        if (onEdit) {
            actions.push(
                el("button", {
                    type: "button",
                    className: "btn-link",
                    text: "Editar",
                    onclick: function () {
                        onEdit(item)
                    },
                }),
            )
        }
        actions.push(
            el("button", {
                type: "button",
                className: "btn-link",
                text: item.is_active ? "Desactivar" : "Activar",
                onclick: toggleActive(path, item),
            }),
            el("button", { type: "button", className: "btn-link", text: "Eliminar", onclick: deleteRecord(path, item, label) }),
        )
        return el("td", { className: "text-right whitespace-nowrap space-x-2" }, actions)
    }

    function loadProducts() {
        return fetchPage("products", "/products/", { search: $("#product-search").value }).then(function (page) {
            renderPage(
                $("#products-body"),
                "products",
                page,
                function (p) {
                    // reorder_level 0 means "no reorder alert" (matches the backend low-stock report).
                    var low = p.is_active && p.reorder_level > 0 && p.total_quantity <= p.reorder_level
                    return el("tr", {}, [
                        td(p.sku, "font-mono"),
                        td(p.barcode, "font-mono"),
                        td(p.lot_tracked ? "Sí" : "No"),
                        td(p.name),
                        td(p.unit),
                        td(p.total_quantity, "text-right" + (low ? " text-red-600 font-medium" : "")),
                        td(p.total_reserved, "text-right"),
                        td(p.total_available, "text-right"),
                        td(p.reorder_level, "text-right"),
                        td(p.is_active ? "Sí" : "No"),
                        actionCell("/products/", p, p.sku, editHandlers.products),
                    ])
                },
                "No se encontraron productos.",
            )
        })
    }

    function loadWarehouses() {
        return fetchPage("warehouses", "/warehouses/").then(function (page) {
            renderPage(
                $("#warehouses-body"),
                "warehouses",
                page,
                function (w) {
                    return el("tr", {}, [
                        td(w.code, "font-mono"),
                        td(w.name),
                        td(w.address),
                        td(w.is_active ? "Sí" : "No"),
                        actionCell("/warehouses/", w, w.code, editHandlers.warehouses),
                    ])
                },
                "Aún no hay almacenes.",
            )
        })
    }

    function loadLots() {
        return fetchPage("lots", "/lots/").then(function (page) {
            renderPage($("#lots-body"), "lots", page, function (lot) {
                return el("tr", {}, [
                    td(lot.product_sku), td(lot.lot_number), td(lot.expiry_date),
                    el("td", { className: "space-x-2" }, [
                        el("button", { type: "button", className: "btn-link", text: "Editar", onclick: function () { editHandlers.lots(lot) } }),
                        el("button", { type: "button", className: "btn-link", text: "Eliminar", onclick: deleteRecord("/lots/", lot, lot.lot_number) }),
                    ]),
                ])
            }, "Aún no hay números de lote.")
        })
    }

    function loadLocations() {
        return fetchPage("locations", "/locations/", { warehouse: $("#location-filter").value }).then(function (page) {
            renderPage(
                $("#locations-body"),
                "locations",
                page,
                function (l) {
                    return el("tr", {}, [
                        td(l.warehouse_code),
                        td(l.code, "font-mono"),
                        td(l.barcode, "font-mono"),
                        td(LOCATION_LABELS[l.location_type] || l.location_type),
                        td(l.is_active ? "Sí" : "No"),
                        actionCell("/locations/", l, l.code, editHandlers.locations),
                    ])
                },
                "Aún no hay ubicaciones.",
            )
        })
    }

    function loadStock() {
        return fetchPage("stock", "/stock/", stockFilters()).then(function (page) {
            renderPage(
                $("#stock-body"),
                "stock",
                page,
                function (s) {
                    return el("tr", {}, [
                        td(s.product_sku, "font-mono"),
                        td(s.product_name),
                        td(s.warehouse_code),
                        td(s.location_code),
                        td(s.lot_number),
                        td(s.expiry_date),
                        td(s.quantity, "text-right font-medium"),
                        td(s.reserved_quantity, "text-right"),
                        td(s.available_quantity, "text-right"),
                        td(formatDate(s.updated_at)),
                    ])
                },
                "No hay existencias.",
            )
        })
    }

    function loadMovements() {
        return Promise.resolve().then(function () {
            return fetchPage("movements", "/movements/", movementFilters())
        }).then(function (page) {
            renderPage(
                $("#movements-body"),
                "movements",
                page,
                function (m) {
                    return el("tr", {}, [
                        td(formatDate(m.occurred_at)),
                        td(formatDate(m.created_at)),
                        el("td", {}, [
                            el("span", { className: "badge badge-" + m.movement_type, text: MOVEMENT_LABELS[m.movement_type] || m.movement_type }),
                        ]),
                        td(m.product_sku, "font-mono"),
                        td(m.lot_number),
                        td(m.expiry_date),
                        td(m.from_location_label),
                        td(m.to_location_label),
                        td(m.quantity, "text-right"),
                        td(m.reference),
                    ])
                },
                "No hay movimientos registrados.",
            )
        })
    }

    function reservationAction(reservation, action) {
        return function (event) {
            var button = event.currentTarget
            button.disabled = true
            Api.post("/reservations/" + reservation.id + "/" + action + "/", {})
                .then(function () {
                    toast(action === "fulfill" ? "Reserva surtida; salida registrada." : "Reserva liberada.")
                    invalidateLookups("/reservations/")
                })
                .then(loadCurrent)
                .catch(showError)
                .finally(function () { button.disabled = false })
        }
    }

    function loadReservations() {
        var labels = { active: "Activa", released: "Liberada", fulfilled: "Surtida" }
        return fetchPage("reservations", "/reservations/", { status: $("#reservation-filter").value }).then(function (page) {
            renderPage($("#reservations-body"), "reservations", page, function (reservation) {
                var actions = []
                if (reservation.status === "active") {
                    actions = [
                        el("button", { type: "button", className: "btn-link", text: "Liberar", onclick: reservationAction(reservation, "release") }),
                        el("button", { type: "button", className: "btn-link", text: "Surtir", onclick: reservationAction(reservation, "fulfill") }),
                    ]
                }
                return el("tr", {}, [
                    td(reservation.reference), td(reservation.product_sku), td(reservation.location_label),
                    td(reservation.lot_number), td(reservation.expiry_date),
                    td(reservation.quantity, "text-right"), td(labels[reservation.status] || reservation.status),
                    td(reservation.movement), td(formatDate(reservation.created_at)),
                    el("td", { className: "space-x-2" }, actions),
                ])
            }, "No hay reservas.")
        })
    }

    var LOADERS = {
        dashboard: loadDashboard,
        products: loadProducts,
        lots: loadLots,
        warehouses: loadWarehouses,
        locations: loadLocations,
        stock: loadStock,
        movements: loadMovements,
        reservations: loadReservations,
    }

    function currentView() {
        var name = window.location.hash.replace("#", "")
        return VIEWS.indexOf(name) === -1 ? "dashboard" : name
    }

    function loadCurrent() {
        return LOADERS[currentView()]().catch(showError)
    }

    function showView() {
        var name = currentView()
        $all(".view").forEach(function (v) {
            v.classList.toggle("hidden", v.id !== "view-" + name)
        })
        $all(".nav-link").forEach(function (a) {
            a.classList.toggle("active", a.getAttribute("data-view") === name)
        })
        loadCurrent()
    }

    // ---------- Forms ----------

    function bindCreateForm(formId, path, successMessage) {
        var form = $(formId)
        var submitButton = form.querySelector('button:not([type]), button[type="submit"], input[type="submit"]')
        var submitting = false
        form.addEventListener("submit", function (event) {
            event.preventDefault()
            if (submitting) return
            submitting = true
            if (submitButton) submitButton.disabled = true
            Api.post(path, formToJson(form))
                .then(function () {
                    toast(successMessage)
                    form.reset()
                    invalidateLookups(path)
                })
                .then(loadCurrent)
                .then(updateMovementForm)
                .catch(showError)
                .finally(function () {
                    submitting = false
                    if (submitButton) submitButton.disabled = false
                })
        })
    }

    function bindEditableForm(formId, path, createTitle, editTitle, createMessage, editMessage) {
        var form = $(formId)
        var title = form.querySelector(".card-title")
        var submitButton = form.querySelector('button:not([type]), button[type="submit"], input[type="submit"]')
        var cancelButton = form.querySelector("[data-cancel-edit]")
        var createButtonText = submitButton.textContent
        var editingId = null
        var submitting = false

        function resetForm() {
            editingId = null
            form.reset()
            title.textContent = createTitle
            submitButton.textContent = createButtonText
            cancelButton.classList.add("hidden")
            var warehouseSelect = form.elements.warehouse
            if (warehouseSelect) lookupControls.get(warehouseSelect).setDisabled(false)
            if (form === $("#lot-form")) lookupControls.get(form.elements.product).setDisabled(false)
        }

        form.addEventListener("submit", function (event) {
            event.preventDefault()
            if (submitting) return
            submitting = true
            submitButton.disabled = true
            cancelButton.disabled = true
            var isEditing = editingId !== null
            var request = isEditing
                ? Api.patch(path + editingId + "/", formToJson(form))
                : Api.post(path, formToJson(form))
            request
                .then(function (item) {
                    toast(isEditing ? editMessage : createMessage)
                    resetForm()
                    invalidateLookups(path, item.id)
                })
                .then(loadCurrent)
                .then(updateMovementForm)
                .catch(showError)
                .finally(function () {
                    submitting = false
                    submitButton.disabled = false
                    cancelButton.disabled = false
                })
        })

        cancelButton.addEventListener("click", resetForm)

        return function (item) {
            editingId = item.id
            title.textContent = editTitle
            submitButton.textContent = "Guardar cambios"
            cancelButton.classList.remove("hidden")
            var warehouseSelect = form.elements.warehouse
            if (warehouseSelect) {
                lookupControls.get(warehouseSelect).setItem({ id: item.warehouse, code: item.warehouse_code })
                lookupControls.get(warehouseSelect).setDisabled(true)
            }
            Object.keys(item).forEach(function (name) {
                var field = form.elements[name]
                if (field && lookupControls.has(field)) {
                    if (name === "product") {
                        lookupControls.get(field).setItem({ id: item.product, sku: item.product_sku })
                        lookupControls.get(field).setDisabled(true)
                    }
                } else if (field && field.type === "checkbox") field.checked = Boolean(item[name])
                else if (field && name !== "warehouse") field.value = item[name] === null ? "" : item[name]
            })
            form.scrollIntoView({ behavior: "smooth", block: "center" })
            var firstField = Array.from(form.elements).find(function (field) {
                return !field.disabled && !field.classList.contains("hidden") && field.tagName !== "BUTTON"
            })
            if (firstField) firstField.focus()
        }
    }

    var MOVEMENT_HINTS = {
        inbound: "Recibe mercancía en la ubicación de destino.",
        outbound: "Prepara o envía mercancía desde la ubicación de origen.",
        transfer: "Mueve mercancía entre dos ubicaciones.",
        adjustment: "Elige un destino para agregar existencias (encontradas) o un origen para quitarlas (perdidas o dañadas), pero no ambos.",
    }

    function updateMovementForm() {
        var type = $("#movement-type").value
        var showFrom = type !== "inbound"
        var showTo = type !== "outbound"
        var from = $('label[data-for="from"]')
        var to = $('label[data-for="to"]')
        from.classList.toggle("hidden", !showFrom)
        to.classList.toggle("hidden", !showTo)
        ;[[from, showFrom], [to, showTo]].forEach(function (pair) {
            var control = lookupControls.get(pair[0].querySelector("select"))
            if (!pair[1]) control.setItem(null)
            control.setDisabled(!pair[1])
        })
        $("#movement-hint").textContent = MOVEMENT_HINTS[type]
        updateMovementLotForm()
    }

    function updateMovementLotForm() {
        var product = lookupControls.get($("#movement-form").elements.product).getItem()
        var tracked = Boolean(product && product.lot_tracked)
        var select = $("#movement-lot")
        var control = lookupControls.get(select)
        var lot = control.getItem()
        if (!tracked || (lot && lot.product !== product.id)) control.setItem(null)
        control.invalidate()
        control.setDisabled(!tracked)
        control.setRequired(tracked)
        $("#movement-lot-field").classList.toggle("hidden", !tracked)
    }

    function checkApi() {
        Api.get("/health/")
            .then(function () {
                $("#api-status").textContent = "API: conectada"
            })
            .catch(function () {
                $("#api-status").textContent = "API: desconectada"
            })
    }

    document.addEventListener("DOMContentLoaded", function () {
        setupLookups()
        editHandlers.products = bindEditableForm(
            "#product-form",
            "/products/",
            "Nuevo producto",
            "Editar producto",
            "Producto creado.",
            "Producto actualizado.",
        )
        editHandlers.warehouses = bindEditableForm(
            "#warehouse-form",
            "/warehouses/",
            "Nuevo almacén",
            "Editar almacén",
            "Almacén creado.",
            "Almacén actualizado.",
        )
        editHandlers.locations = bindEditableForm(
            "#location-form",
            "/locations/",
            "Nueva ubicación",
            "Editar ubicación",
            "Ubicación creada.",
            "Ubicación actualizada.",
        )
        editHandlers.lots = bindEditableForm(
            "#lot-form", "/lots/", "Nuevo número de lote", "Editar número de lote", "Número de lote creado.", "Número de lote actualizado.",
        )
        bindCreateForm("#movement-form", "/movements/", "Movimiento registrado.")
        bindCreateForm("#reservation-form", "/reservations/", "Reserva creada.")
        bindCsvExport("#stock-export", "/stock/", stockFilters, "#stock-body", "existencias.csv", function (s) {
            return [
                s.product_sku, s.product_name, s.warehouse_code, s.location_code, s.lot_number,
                s.expiry_date, s.quantity, s.reserved_quantity, s.available_quantity, s.updated_at,
            ]
        })
        bindCsvExport("#movement-export", "/movements/", movementFilters, "#movements-body", "movimientos.csv", function (m) {
            return [
                m.occurred_at, m.created_at, MOVEMENT_LABELS[m.movement_type] || m.movement_type,
                m.product_sku, m.lot_number, m.expiry_date, m.from_location_label,
                m.to_location_label, m.quantity, m.reference,
            ]
        })

        $("#movement-type").addEventListener("change", updateMovementForm)
        $("#movement-form").elements.product.addEventListener("change", updateMovementLotForm)
        function filterChanged(view) {
            return function () {
                resetPage(view)
                loadCurrent()
            }
        }
        $("#product-search").addEventListener("input", debounce(filterChanged("products"), 300))
        $("#stock-search").addEventListener("input", debounce(filterChanged("stock"), 300))
        $("#stock-warehouse").addEventListener("change", filterChanged("stock"))
        $("#location-filter").addEventListener("change", filterChanged("locations"))
        $("#movement-filter").addEventListener("change", filterChanged("movements"))
        $("#movement-search").addEventListener("input", debounce(filterChanged("movements"), 300))
        $("#movement-date-from").addEventListener("change", filterChanged("movements"))
        $("#movement-date-to").addEventListener("change", filterChanged("movements"))
        $("#reservation-filter").addEventListener("change", filterChanged("reservations"))
        window.addEventListener("hashchange", showView)

        updateMovementForm()
        checkApi()
        showView()
    })
})()
