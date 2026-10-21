// ============================================================
// BILLING - FINAL APP.JS
// ============================================================

let products = [];
let cart = [];
let selectedProduct = null;
let editingCartId = null;


// ============================================================
// PAGE INITIALIZATION
// ============================================================

document.addEventListener("DOMContentLoaded", async function () {

    console.log("Billing JS started");

    setupEvents();

    await loadProducts();

    // Check if we need to restore a cart (e.g. Back from invoice or Re-opened from history)
    const restoreData = sessionStorage.getItem("restore_cart");
    if (restoreData) {
        try {
            const parsed = JSON.parse(restoreData);
            sessionStorage.removeItem("restore_cart");

            if (parsed.items && Array.isArray(parsed.items) && parsed.items.length > 0) {
                cart = parsed.items;

                const nameInput = document.getElementById("customerName");
                if (nameInput && parsed.customer_name) {
                    nameInput.value = parsed.customer_name;
                }

                const phoneInput = document.getElementById("customerPhone");
                if (phoneInput && parsed.customer_phone) {
                    phoneInput.value = parsed.customer_phone;
                }

                const payInput = document.getElementById("paymentMethod");
                if (payInput && parsed.payment_method) {
                    payInput.value = parsed.payment_method;
                }

                const discChk = document.getElementById("enableDiscountCheckbox");
                const discArea = document.getElementById("cartDiscountInputArea");
                const discType = document.getElementById("cartDiscountType");
                const discVal = document.getElementById("cartDiscountValue");
                const discUnit = document.getElementById("cartDiscountUnitLabel");
                const dp = document.getElementById("cartDiscountPercent");
                const da = document.getElementById("cartDiscountAmount");

                if (parsed.discount_percent && Number(parsed.discount_percent) > 0) {
                    if (discChk) discChk.checked = true;
                    if (discArea) discArea.style.display = "block";
                    if (discType) discType.value = "percent";
                    if (discVal) {
                        discVal.value = parsed.discount_percent;
                        discVal.max = "100";
                    }
                    if (discUnit) discUnit.textContent = "%";
                    if (dp) dp.value = parsed.discount_percent;
                    if (da) da.value = "0";
                } else if (parsed.flat_discount && Number(parsed.flat_discount) > 0) {
                    if (discChk) discChk.checked = true;
                    if (discArea) discArea.style.display = "block";
                    if (discType) discType.value = "flat";
                    if (discVal) {
                        discVal.value = parsed.flat_discount;
                        discVal.removeAttribute("max");
                    }
                    if (discUnit) discUnit.textContent = "₹";
                    if (dp) dp.value = "0";
                    if (da) da.value = parsed.flat_discount;
                }

                if (typeof lookupCustomerPending === "function") {
                    lookupCustomerPending();
                }

                console.log("Cart restored:", cart);
            }
        } catch (err) {
            console.error("Error restoring cart:", err);
        }
    }

    renderCart();
});


// ============================================================
// SETUP EVENTS
// ============================================================

function setupEvents() {

    const searchInput =
        document.getElementById("productSearch");

    const quantityInput =
        document.getElementById("quantityValue");

    const quantityUnit =
        document.getElementById("quantityUnit");

    // Product search
    if (searchInput) {

        searchInput.addEventListener(
            "input",
            searchProducts
        );

        searchInput.addEventListener(
            "focus",
            function () {

                if (searchInput.value.trim()) {
                    searchProducts();
                }

            }
        );
    }

    // Quantity
    if (quantityInput) {

        quantityInput.addEventListener(
            "input",
            calculateItemTotal
        );
    }

    if (quantityUnit) {

        quantityUnit.addEventListener(
            "change",
            calculateItemTotal
        );
    }

    // Edit modal
    const editModal =
        document.getElementById("editModal");

    if (editModal) {

        editModal.addEventListener(
            "click",
            function (event) {

                if (event.target === editModal) {
                    closeEditModal();
                }

            }
        );
    }
}


// ============================================================
// LOAD PRODUCTS
// ============================================================

async function loadProducts() {

    try {

        const response =
            await fetch("/api/products", {
                method: "GET",
                credentials: "same-origin"
            });

        if (response.status === 401) {

            window.location.href = "/";

            return;
        }

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.error ||
                "Unable to load products"
            );
        }

        if (!Array.isArray(data)) {

            throw new Error(
                "Invalid product data received"
            );
        }

        products = data;

        console.log(
            "Products loaded:",
            products
        );

    } catch (error) {

        console.error(
            "Product loading error:",
            error
        );

        products = [];

        alert(
            "Unable to load products.\n\n" +
            error.message
        );
    }
}


// ============================================================
// SEARCH PRODUCTS
// ============================================================

function searchProducts() {

    const searchInput =
        document.getElementById(
            "productSearch"
        );

    const searchResults =
        document.getElementById(
            "searchResults"
        );

    if (!searchInput || !searchResults) {

        console.error(
            "Search elements not found"
        );

        return;
    }

    const search =
        searchInput.value
            .trim()
            .toLowerCase();


    // Nothing typed
    if (!search) {

        searchResults.innerHTML = "";

        searchResults.classList.remove(
            "active"
        );

        return;
    }


    // Find matching products
    const matches =
        products.filter(function (product) {

            const productName =
                String(
                    product.name || ""
                ).toLowerCase();

            const billingName =
                String(
                    product.billing_name || ""
                ).toLowerCase();

            return (
                productName.includes(search) ||
                billingName.includes(search)
            );
        });


    console.log(
        "Search:",
        search,
        "Matches:",
        matches
    );


    // No match
    if (matches.length === 0) {

        searchResults.innerHTML = `
            <div class="search-empty">
                <span>🔍</span>

                <div>
                    <strong>No product found</strong>
                    <small>
                        Try another product name
                    </small>
                </div>
            </div>
        `;

        searchResults.classList.add(
            "active"
        );

        return;
    }


    // Create results
    searchResults.innerHTML =
        matches.map(function (product) {

            const stock =
                Number(
                    product.stock_quantity || 0
                );

            const price =
                Number(
                    product.price || 0
                );

            const basis =
                product.price_basis ||
                "Piece";


            // IMPORTANT:
            // Don't show the same name twice.
            let displayName =
                product.billing_name ||
                product.name ||
                "Product";


            let secondaryName = "";


            // Only show product name
            // when it is actually different
            if (
                product.name &&
                product.billing_name &&
                product.name.trim().toLowerCase() !==
                product.billing_name.trim().toLowerCase()
            ) {

                secondaryName =
                    product.name;
            }


            const discountPercent = Number(product.discount_percent || 0);
            const effectivePrice = discountPercent > 0
                ? Math.round(price * (1 - discountPercent / 100) * 100) / 100
                : price;

            return `
                <button
                    type="button"
                    class="search-result-item"
                    onclick="selectProduct(${Number(product.id)})"
                    ${stock <= 0 ? "disabled" : ""}
                >

                    <div class="search-result-icon">
                        📦
                    </div>

                    <div class="search-result-info">

                        <strong>
                            ${escapeHtml(displayName)}
                        </strong>

                        ${
                            secondaryName
                                ? `
                                    <span>
                                        ${escapeHtml(
                                            secondaryName
                                        )}
                                    </span>
                                  `
                                : ""
                        }

                    </div>

                    <div class="search-result-price">

                        ${discountPercent > 0 ? `
                            <strong style="color: #16a34a;">
                                ₹${formatMoney(effectivePrice)}
                            </strong>
                            <span style="text-decoration: line-through; color: #9ca3af; font-size: 11px; margin-left: 3px;">
                                ₹${formatMoney(price)}
                            </span>
                            <span style="background: #dcfce7; color: #15803d; font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 4px; margin-left: 3px;">
                                🏷️ ${discountPercent}% OFF
                            </span>
                        ` : `
                            <strong>
                                ₹${formatMoney(price)}
                            </strong>
                        `}

                        <span>
                            / ${escapeHtml(basis)}
                        </span>

                    </div>

                    <div
                        class="
                            search-result-stock
                            ${stock <= 0 ? "out-stock" : ""}
                        "
                    >

                        ${
                            stock <= 0
                                ? "Out of stock"
                                : `
                                    Stock:
                                    ${formatQuantity(stock)}
                                    ${escapeHtml(basis)}
                                  `
                        }

                    </div>

                </button>
            `;

        }).join("");


    searchResults.classList.add(
        "active"
    );
}


// ============================================================
// SELECT PRODUCT
// ============================================================

function selectProduct(productId) {

    console.log(
        "Selecting product:",
        productId
    );


    // IMPORTANT:
    // HTML passes product ID
    selectedProduct =
        products.find(function (product) {

            return Number(product.id) ===
                Number(productId);

        });


    if (!selectedProduct) {

        console.error(
            "Product not found:",
            productId
        );

        alert(
            "Unable to select this product."
        );

        return;
    }


    console.log(
        "Selected product:",
        selectedProduct
    );


    // --------------------------------------------------------
    // Hide search results
    // --------------------------------------------------------

    const searchResults =
        document.getElementById(
            "searchResults"
        );

    if (searchResults) {

        searchResults.innerHTML = "";

        searchResults.classList.remove(
            "active"
        );

        searchResults.style.display =
            "none";
    }


    // --------------------------------------------------------
    // Put product name in search box
    // --------------------------------------------------------

    const searchInput =
        document.getElementById(
            "productSearch"
        );

    if (searchInput) {

        searchInput.value =
            selectedProduct.billing_name ||
            selectedProduct.name;
    }


    // --------------------------------------------------------
    // SHOW SELECTED PRODUCT
    // --------------------------------------------------------

    const selectedSection =
        document.getElementById(
            "selectedProduct"
        );

    if (selectedSection) {

        selectedSection.style.display =
            "block";

        selectedSection.classList.add(
            "selected-product-animation"
        );
    }


    // --------------------------------------------------------
    // Product information
    // --------------------------------------------------------

    const discountPercent = Number(selectedProduct.discount_percent || 0);
    const rawPrice = Number(selectedProduct.price || 0);
    const effectivePrice = discountPercent > 0
        ? Math.round(rawPrice * (1 - discountPercent / 100) * 100) / 100
        : rawPrice;

    setText(
        "selectedBillingName",
        selectedProduct.billing_name ||
        selectedProduct.name
    );

    setText(
        "selectedPrice",
        `₹${formatMoney(effectivePrice)}`
    );

    const discountCard = document.getElementById("selectedDiscountCard");
    if (discountCard) {
        if (discountPercent > 0) {
            discountCard.style.display = "block";
            setText("selectedDiscount", `${discountPercent}% OFF`);
        } else {
            discountCard.style.display = "none";
        }
    }

    setText(
        "selectedBasis",
        selectedProduct.price_basis ||
        "Piece"
    );

    setText(
        "selectedStock",
        `${formatQuantity(
            selectedProduct.stock_quantity
        )} ${
            selectedProduct.price_basis ||
            "Piece"
        }`
    );

    // --------------------------------------------------------
    // Quantity units
    // --------------------------------------------------------

    setupQuantityUnits();

    // --------------------------------------------------------
    // Reset quantity
    // --------------------------------------------------------

    const quantity =
        document.getElementById(
            "quantityValue"
        );

    if (quantity) {

        quantity.value = "";

        quantity.focus();
    }

    // --------------------------------------------------------
    // Reset calculation
    // --------------------------------------------------------

    setText(
        "displayQuantity",
        "0"
    );

    setText(
        "displayUnitPrice",
        `₹${formatMoney(
            effectivePrice
        )} / ${
            selectedProduct.price_basis
        }`
    );

    setText(
        "displayItemTotal",
        "₹0.00"
    );


    const addButton =
        document.getElementById(
            "addToCartButton"
        );

    if (addButton) {
        addButton.disabled = true;
    }
}


// ============================================================
// QUANTITY UNITS
// ============================================================

function setupQuantityUnits() {

    const select =
        document.getElementById(
            "quantityUnit"
        );

    if (!select || !selectedProduct) {
        return;
    }


    const basis =
        String(
            selectedProduct.price_basis ||
            "Piece"
        ).trim().toLowerCase();


    select.innerHTML = "";


    // KG
    if (basis === "kg") {

        select.innerHTML = `
            <option value="KG">KG</option>
            <option value="Gram">Gram</option>
        `;

        return;
    }


    // Gram
    if (basis === "gram") {

        select.innerHTML = `
            <option value="Gram">Gram</option>
        `;

        return;
    }


    // Litre
    if (
        basis === "litre" ||
        basis === "liter" ||
        basis === "litres" ||
        basis === "liters"
    ) {

        select.innerHTML = `
            <option value="Litre">Litre</option>
            <option value="ML">ML</option>
        `;

        return;
    }


    // ML
    if (basis === "ml") {

        select.innerHTML = `
            <option value="ML">ML</option>
        `;

        return;
    }


    // Piece
    select.innerHTML = `
        <option value="Piece">Piece</option>
    `;
}


// ============================================================
// CONVERT TO BASE QUANTITY
// ============================================================

function convertToBaseQuantity(
    quantity,
    unit
) {

    if (!selectedProduct) {
        return 0;
    }


    const value =
        Number(quantity);


    if (
        !Number.isFinite(value) ||
        value <= 0
    ) {
        return 0;
    }


    const basis =
        String(
            selectedProduct.price_basis ||
            "Piece"
        ).trim().toLowerCase();


    const selectedUnit =
        String(
            unit || ""
        ).trim().toLowerCase();


    // KG
    if (basis === "kg") {

        if (
            selectedUnit === "gram" ||
            selectedUnit === "g"
        ) {

            return value / 1000;
        }

        return value;
    }


    // Gram
    if (basis === "gram") {

        if (
            selectedUnit === "kg"
        ) {

            return value * 1000;
        }

        return value;
    }


    // Litre
    if (
        basis === "litre" ||
        basis === "liter" ||
        basis === "litres" ||
        basis === "liters"
    ) {

        if (
            selectedUnit === "ml"
        ) {

            return value / 1000;
        }

        return value;
    }


    // ML
    if (basis === "ml") {

        if (
            selectedUnit === "litre" ||
            selectedUnit === "liter"
        ) {

            return value * 1000;
        }

        return value;
    }


    // Piece
    return value;
}


// ============================================================
// CALCULATE ITEM
// ============================================================

function calculateItemTotal() {

    if (!selectedProduct) {
        return null;
    }


    const quantityInput =
        document.getElementById(
            "quantityValue"
        );

    const unitInput =
        document.getElementById(
            "quantityUnit"
        );


    const quantity =
        Number(
            quantityInput?.value || 0
        );


    const unit =
        unitInput?.value ||
        selectedProduct.price_basis ||
        "Piece";


    const baseQuantity =
        convertToBaseQuantity(
            quantity,
            unit
        );


    const stock =
        Number(
            selectedProduct.stock_quantity ||
            0
        );


    const discountPercent =
        Number(
            selectedProduct.discount_percent || 0
        );

    const rawPrice =
        Number(
            selectedProduct.price || 0
        );

    const price =
        discountPercent > 0
            ? Math.round(rawPrice * (1 - discountPercent / 100) * 100) / 100
            : rawPrice;


    // Already in cart
    const alreadyInCart =
        cart
            .filter(function (item) {

                return Number(item.productId) ===
                    Number(selectedProduct.id);

            })
            .reduce(function (
                total,
                item
            ) {

                return total +
                    Number(
                        item.baseQuantity || 0
                    );

            }, 0);


    // Stock validation
    if (
        baseQuantity > 0 &&
        alreadyInCart +
        baseQuantity > stock
    ) {

        const available =
            Math.max(
                stock - alreadyInCart,
                0
            );


        setText(
            "displayQuantity",
            formatQuantity(quantity)
        );

        setText(
            "displayUnitPrice",
            `₹${formatMoney(
                price
            )} / ${
                selectedProduct.price_basis
            }`
        );

        setText(
            "displayItemTotal",
            "₹0.00"
        );


        const addButton =
            document.getElementById(
                "addToCartButton"
            );

        if (addButton) {
            addButton.disabled = true;
        }


        return {
            quantity,
            unit,
            baseQuantity,
            total: 0,
            error:
                `Only ${formatQuantity(
                    available
                )} ${
                    selectedProduct.price_basis
                } available.`
        };
    }


    const total =
        baseQuantity * price;


    // Update UI
    setText(
        "displayQuantity",
        quantity > 0
            ? `${formatQuantity(quantity)} ${unit}`
            : "0"
    );


    setText(
        "displayUnitPrice",
        `₹${formatMoney(
            price
        )} / ${
            selectedProduct.price_basis
        }`
    );


    setText(
        "displayItemTotal",
        `₹${formatMoney(total)}`
    );


    const addButton =
        document.getElementById(
            "addToCartButton"
        );


    if (addButton) {

        addButton.disabled =
            quantity <= 0 ||
            baseQuantity <= 0;
    }


    return {
        quantity,
        unit,
        baseQuantity,
        total
    };
}


// ============================================================
// BATCH TRANSITION / SPLIT PRICING MODAL
// ============================================================

let pendingBatchSplit = null;

function showBatchTransitionModal(product, activeBatches, newTotalBase, result) {
    const rem1 = Number(activeBatches[0].quantity_remaining || 0);
    const take2 = Math.round((newTotalBase - rem1) * 1000) / 1000;
    const p1 = Number(activeBatches[0].effective_price || activeBatches[0].selling_price);
    const p2 = Number(activeBatches[1].effective_price || activeBatches[1].selling_price);
    const basis = product.price_basis || "Piece";

    pendingBatchSplit = {
        product,
        activeBatches,
        newTotalBase,
        result,
        rem1,
        take2,
        p1,
        p2
    };

    const titleEl = document.getElementById("batchTransitionTitle");
    if (titleEl) {
        titleEl.textContent = `${product.billing_name || product.name} — Batch Transition`;
    }

    const msgEl = document.getElementById("batchTransitionMsg");
    if (msgEl) {
        msgEl.innerHTML = `
            <strong>Batch #1 has finished / has only ${formatQuantity(rem1)} ${basis} remaining</strong> at <strong>₹${formatMoney(p1)}</strong>.<br>
            The remaining <strong>${formatQuantity(take2)} ${basis}</strong> will be taken from <strong>Batch #2</strong> (standard rate: <strong>₹${formatMoney(p2)}</strong>).
        `;
    }

    const sepDesc = document.getElementById("batchChoiceSeparateDesc");
    if (sepDesc) {
        const totalSep = Math.round(((rem1 * p1) + (take2 * p2)) * 100) / 100;
        sepDesc.textContent = `${formatQuantity(rem1)} from Batch #1 @ ₹${formatMoney(p1)} + ${formatQuantity(take2)} from Batch #2 @ ₹${formatMoney(p2)} (Total: ₹${formatMoney(totalSep)})`;
    }

    const matchDesc = document.getElementById("batchChoiceMatchDesc");
    if (matchDesc) {
        const totalMatch = Math.round((newTotalBase * p1) * 100) / 100;
        matchDesc.textContent = `Give all ${formatQuantity(newTotalBase)} ${basis} at Batch #1 rate of ₹${formatMoney(p1)} (Total: ₹${formatMoney(totalMatch)})`;
    }

    const modal = document.getElementById("batchTransitionModal");
    if (modal) {
        modal.style.display = "flex";
    }
}

function applyBatchChoice(choice) {
    if (!pendingBatchSplit) {
        closeBatchTransitionModal();
        return;
    }

    const { product, activeBatches, newTotalBase, result, rem1, take2, p1, p2 } = pendingBatchSplit;
    const unitMultiplier = result.baseQuantity > 0 ? (result.quantity / result.baseQuantity) : 1;

    // Clear old items for this product
    cart = cart.filter(it => Number(it.productId) !== Number(product.id));

    // Batch 1 item
    const takeDisplay1 = Math.round(rem1 * unitMultiplier * 1000) / 1000;
    cart.push({
        id: Date.now() + Math.random(),
        productId: Number(product.id),
        batchId: activeBatches[0].id,
        productName: product.name,
        billingName: `${product.billing_name || product.name} (Batch #${activeBatches[0].id})`,
        quantity: takeDisplay1,
        unit: result.unit,
        baseQuantity: rem1,
        price: p1,
        originalPrice: Number(activeBatches[0].selling_price),
        discountPercent: Number(activeBatches[0].discount_percent || 0),
        priceBasis: product.price_basis,
        total: Math.round(rem1 * p1 * 100) / 100
    });

    // Batch 2 item
    const takeDisplay2 = Math.round(take2 * unitMultiplier * 1000) / 1000;
    const finalP2 = choice === "match" ? p1 : p2;
    cart.push({
        id: Date.now() + Math.random() + 1,
        productId: Number(product.id),
        batchId: activeBatches[1].id,
        productName: product.name,
        billingName: `${product.billing_name || product.name} (Batch #${activeBatches[1].id})`,
        quantity: takeDisplay2,
        unit: result.unit,
        baseQuantity: take2,
        price: finalP2,
        originalPrice: Number(activeBatches[1].selling_price),
        discountPercent: choice === "match" ? 0 : Number(activeBatches[1].discount_percent || 0),
        priceBasis: product.price_basis,
        total: Math.round(take2 * finalP2 * 100) / 100
    });

    closeBatchTransitionModal();
    renderCart();
    clearSelectedProduct();
}

function closeBatchTransitionModal() {
    const modal = document.getElementById("batchTransitionModal");
    if (modal) {
        modal.style.display = "none";
    }
    pendingBatchSplit = null;
}

function matchBatch1Price(productId) {
    const prodItems = cart.filter(it => Number(it.productId) === Number(productId));
    if (prodItems.length < 2) return;
    const targetPrice = prodItems[0].price;
    for (let i = 1; i < prodItems.length; i++) {
        prodItems[i].price = targetPrice;
        prodItems[i].total = Math.round(prodItems[i].baseQuantity * targetPrice * 100) / 100;
    }
    renderCart();
}


// ============================================================
// ADD TO CART
// ============================================================

function addToCart() {

    if (!selectedProduct) {

        alert(
            "Please select a product first."
        );

        return;
    }


    const result =
        calculateItemTotal();


    if (!result) {

        return;
    }


    if (
        result.error
    ) {

        alert(
            result.error
        );

        return;
    }


    if (
        result.quantity <= 0 ||
        result.baseQuantity <= 0
    ) {

        alert(
            "Please enter a valid quantity."
        );

        return;
    }


    // Check stock again
    if (
        result.baseQuantity >
        Number(
            selectedProduct.stock_quantity
        )
    ) {

        alert(
            "Insufficient stock."
        );

        return;
    }


    const activeBatches = (selectedProduct.batches || []).filter(b => Number(b.quantity_remaining) > 0);

    // Check if total exceeds stock
    const existingInCart = cart.filter(it => Number(it.productId) === Number(selectedProduct.id));
    const totalCartBase = existingInCart.reduce((s, it) => s + Number(it.baseQuantity || 0), 0);
    const newTotalBase = totalCartBase + Number(result.baseQuantity);

    if (newTotalBase > Number(selectedProduct.stock_quantity)) {
        alert("The total quantity exceeds available stock (" + formatQuantity(selectedProduct.stock_quantity) + " available).");
        return;
    }

    if (activeBatches.length > 1 && newTotalBase > Number(activeBatches[0].quantity_remaining)) {
        const rem1 = Number(activeBatches[0].quantity_remaining || 0);
        const p1 = Number(activeBatches[0].effective_price || activeBatches[0].selling_price);
        const p2 = Number(activeBatches[1].effective_price || activeBatches[1].selling_price);

        if (p1 !== p2) {
            showBatchTransitionModal(selectedProduct, activeBatches, newTotalBase, result);
            return;
        }

        // If prices are the same, split directly
        cart = cart.filter(it => Number(it.productId) !== Number(selectedProduct.id));

        let remNeeded = newTotalBase;
        const unitMultiplier = result.baseQuantity > 0 ? (result.quantity / result.baseQuantity) : 1;

        activeBatches.forEach((b) => {
            if (remNeeded <= 0) return;
            const bRem = Number(b.quantity_remaining || 0);
            const takeBase = Math.min(remNeeded, bRem);
            if (takeBase > 0) {
                const takeDisplay = Math.round(takeBase * unitMultiplier * 1000) / 1000;
                const effPrice = Number(b.effective_price || b.selling_price);
                const rawPrice = Number(b.selling_price);
                const discPct = Number(b.discount_percent || 0);

                cart.push({
                    id: Date.now() + Math.random(),
                    productId: Number(selectedProduct.id),
                    batchId: b.id,
                    productName: selectedProduct.name,
                    billingName: `${selectedProduct.billing_name || selectedProduct.name} (Batch #${b.id})`,
                    quantity: takeDisplay,
                    unit: result.unit,
                    baseQuantity: takeBase,
                    price: effPrice,
                    originalPrice: rawPrice,
                    discountPercent: discPct,
                    priceBasis: selectedProduct.price_basis,
                    total: Math.round(takeBase * effPrice * 100) / 100
                });
                remNeeded -= takeBase;
            }
        });

        // Any remaining quantity fallback
        if (remNeeded > 0) {
            const takeDisplay = Math.round(remNeeded * unitMultiplier * 1000) / 1000;
            const effPrice = Number(selectedProduct.price || 0);
            cart.push({
                id: Date.now() + Math.random(),
                productId: Number(selectedProduct.id),
                batchId: null,
                productName: selectedProduct.name,
                billingName: selectedProduct.billing_name || selectedProduct.name,
                quantity: takeDisplay,
                unit: result.unit,
                baseQuantity: remNeeded,
                price: effPrice,
                originalPrice: effPrice,
                discountPercent: Number(selectedProduct.discount_percent || 0),
                priceBasis: selectedProduct.price_basis,
                total: Math.round(remNeeded * effPrice * 100) / 100
            });
        }
    } else {
        const activeBatch = activeBatches.length > 0 ? activeBatches[0] : null;
        const discPct = activeBatch ? Number(activeBatch.discount_percent || 0) : Number(selectedProduct.discount_percent || 0);
        const rawP = activeBatch ? Number(activeBatch.selling_price || 0) : Number(selectedProduct.price || 0);
        const effectiveP = activeBatch ? Number(activeBatch.effective_price || rawP) : (discPct > 0 ? Math.round(rawP * (1 - discPct / 100) * 100) / 100 : rawP);

        const existing = cart.find(it => Number(it.productId) === Number(selectedProduct.id));

        if (existing) {
            existing.quantity = Number(existing.quantity) + Number(result.quantity);
            existing.baseQuantity = newTotalBase;
            existing.price = effectiveP;
            existing.originalPrice = rawP;
            existing.discountPercent = discPct;
            existing.total = Math.round(newTotalBase * effectiveP * 100) / 100;
        } else {
            cart.push({
                id: Date.now() + Math.random(),
                productId: Number(selectedProduct.id),
                batchId: activeBatch ? activeBatch.id : null,
                productName: selectedProduct.name,
                billingName: selectedProduct.billing_name || selectedProduct.name,
                quantity: Number(result.quantity),
                unit: result.unit,
                baseQuantity: Number(result.baseQuantity),
                price: effectiveP,
                originalPrice: rawP,
                discountPercent: discPct,
                priceBasis: selectedProduct.price_basis,
                total: Math.round(Number(result.baseQuantity) * effectiveP * 100) / 100
            });
        }
    }


    console.log(
        "Cart:",
        cart
    );


    renderCart();

    clearSelectedProduct();
}


// ============================================================
// RENDER CART
// ============================================================

function renderCart() {

    const container =
        document.getElementById(
            "cartItems"
        );

    const badge =
        document.getElementById(
            "cartBadge"
        );

    const cartFooter =
        document.getElementById(
            "cartFooter"
        );

    const summaryItems =
        document.getElementById(
            "summaryItems"
        );

    const summarySubtotal =
        document.getElementById(
            "summarySubtotal"
        );

    const grandTotal =
        document.getElementById(
            "grandTotal"
        );

    const generateButton =
        document.getElementById(
            "generateBillButton"
        );


    if (!container) {

        console.error(
            "cartItems element not found"
        );

        return;
    }


    // EMPTY CART
    if (!cart.length) {

        container.innerHTML = `
            <div class="cart-empty">

                <div class="empty-cart-icon">
                    🛒
                </div>

                <h3>
                    Your cart is empty
                </h3>

                <p>
                    Search and add products to begin.
                </p>

            </div>
        `;


        if (badge) {
            badge.textContent = "0";
        }


        if (summaryItems) {
            summaryItems.textContent = "0";
        }


        if (summarySubtotal) {
            summarySubtotal.textContent =
                "₹0.00";
        }


        if (grandTotal) {
            grandTotal.textContent =
                "₹0.00";
        }


        if (cartFooter) {
            cartFooter.style.display =
                "none";
        }


        if (generateButton) {
            generateButton.disabled =
                true;
        }


        return;
    }


    // CART ITEMS
    container.innerHTML =
        cart.map(function (item) {

            const sameProdItems = item.productId ? cart.filter(it => Number(it.productId) === Number(item.productId)) : [];
            const canMatchBatch1 = sameProdItems.length > 1 && sameProdItems[0].id !== item.id && item.price !== sameProdItems[0].price;

            return `
                <div class="cart-item">

                    <div class="cart-item-info">

                        <strong>
                            ${escapeHtml(
                                item.billingName
                            )}
                        </strong>

                        <span>
                            ${formatQuantity(
                                item.quantity
                            )}
                            ${escapeHtml(
                                item.unit
                            )}
                            ×
                            ${Number(item.discountPercent || 0) > 0 ? `
                                <strong style="color: #16a34a;">₹${formatMoney(item.price)}</strong>
                                <span style="text-decoration: line-through; color: #9ca3af; font-size: 11px; margin-left: 2px;">₹${formatMoney(item.originalPrice || item.price)}</span>
                                <span style="background: #fef3c7; color: #92400e; font-size: 10px; font-weight: 700; padding: 1px 4px; border-radius: 4px; margin-left: 2px;">🏷️ ${Number(item.discountPercent)}% OFF</span>
                            ` : `
                                ₹${formatMoney(
                                    item.price
                                )}
                            `}
                            / ${
                                escapeHtml(
                                    item.priceBasis
                                )
                            }
                        </span>

                    </div>


                    <div class="cart-item-total">
                        ₹${formatMoney(
                            item.total
                        )}
                    </div>


                    <div class="cart-item-actions">

                        ${canMatchBatch1 ? `
                        <button
                            type="button"
                            title="Match Batch #1 price (₹${formatMoney(sameProdItems[0].price)})"
                            style="background:#eff6ff; color:#2563eb; border:1px solid #bfdbfe; font-size:11px; padding:3px 7px; border-radius:6px; cursor:pointer;"
                            onclick="matchBatch1Price(${item.productId})"
                        >
                            ⭐ Match Batch 1 Rate
                        </button>
                        ` : ""}

                        <button
                            type="button"
                            onclick="openEditCart(${item.id})"
                        >
                            Edit
                        </button>

                        <button
                            type="button"
                            onclick="removeCartItem(${item.id})"
                        >
                            Remove
                        </button>

                    </div>

                </div>
            `;

        }).join("");


    // TOTAL (Subtotal)
    const subtotal =
        cart.reduce(function (
            sum,
            item
        ) {

            return sum +
                Number(
                    item.total || 0
                );

        }, 0);


    // Badge
    if (badge) {

        badge.textContent =
            String(cart.length);
    }


    // Summary
    if (summaryItems) {

        summaryItems.textContent =
            String(cart.length);
    }


    if (summarySubtotal) {

        summarySubtotal.textContent =
            `₹${formatMoney(subtotal)}`;
    }


    // CART DISCOUNT (Percentage % OR Direct Flat Rupee Less ₹)
    // If any product in cart already has a product-level discount, do NOT allow another cart-level discount
    const hasProductLevelDiscount = cart.some(function (item) {
        if (Number(item.discountPercent || 0) > 0) return true;
        const p = products.find(function (prod) { return Number(prod.id) === Number(item.productId); });
        return p && Number(p.discount_percent || 0) > 0;
    });

    const cartDiscountCard = document.getElementById("cartDiscountSection");
    const chk = document.getElementById("enableDiscountCheckbox");
    const checkContainer = document.getElementById("cartDiscountCheckboxContainer");
    const notice = document.getElementById("cartDiscountDisabledNotice");
    const inputArea = document.getElementById("cartDiscountInputArea");
    const valInput = document.getElementById("cartDiscountValue");
    const feedback = document.getElementById("cartDiscountLiveFeedback");

    if (cartDiscountCard) {
        if (hasProductLevelDiscount) {
            if (notice) notice.style.display = "block";
            if (checkContainer) checkContainer.style.display = "none";
            if (chk) chk.checked = false;
            if (inputArea) inputArea.style.display = "none";
            if (valInput) valInput.value = "";
            const dp = document.getElementById("cartDiscountPercent");
            if (dp) dp.value = "0";
            const da = document.getElementById("cartDiscountAmount");
            if (da) da.value = "0";
            if (feedback) feedback.textContent = "";
        } else {
            if (notice) notice.style.display = "none";
            if (checkContainer) checkContainer.style.display = "flex";
        }
    }

    const isDiscountEnabled = chk && chk.checked && !hasProductLevelDiscount;
    const discountPctInput = document.getElementById("cartDiscountPercent");
    const discountAmtInput = document.getElementById("cartDiscountAmount");
    const cartDiscountPercent = isDiscountEnabled
        ? Math.max(0, Math.min(100, parseFloat(discountPctInput?.value || 0) || 0))
        : 0;
    const cartDiscountAmount = isDiscountEnabled
        ? Math.max(0, parseFloat(discountAmtInput?.value || 0) || 0)
        : 0;

    const pctDiscountVal = Math.round((subtotal * (cartDiscountPercent / 100)) * 100) / 100;
    const totalDiscount = Math.min(subtotal, Math.round((pctDiscountVal + cartDiscountAmount) * 100) / 100);
    const discountedSubtotal = Math.max(0, Math.round((subtotal - totalDiscount) * 100) / 100);

    const discountLine = document.getElementById("discountSummaryLine");
    const discountAmountEl = document.getElementById("summaryDiscountAmount");
    const discountBadge = document.getElementById("cartDiscountDisplayBadge");

    if (feedback) {
        if (isDiscountEnabled) {
            if (totalDiscount > 0) {
                feedback.textContent = cartDiscountPercent > 0
                    ? `✓ ${cartDiscountPercent}% discount applied (-₹${formatMoney(pctDiscountVal)})`
                    : `✓ ₹${formatMoney(cartDiscountAmount)} flat discount applied`;
            } else {
                feedback.textContent = "Enter discount value above to reduce from bill";
            }
        } else {
            feedback.textContent = "";
        }
    }

    if (totalDiscount > 0) {
        if (discountLine) discountLine.style.display = "flex";
        if (discountAmountEl) discountAmountEl.textContent = `-₹${formatMoney(totalDiscount)}`;
        if (discountBadge) {
            discountBadge.style.display = "inline-block";
            let badgeText = "";
            if (cartDiscountPercent > 0 && cartDiscountAmount > 0) {
                badgeText = `${cartDiscountPercent}% + ₹${formatMoney(cartDiscountAmount)} LESS`;
            } else if (cartDiscountPercent > 0) {
                badgeText = `${cartDiscountPercent}% OFF`;
            } else {
                badgeText = `-₹${formatMoney(cartDiscountAmount)} LESS`;
            }
            discountBadge.textContent = badgeText;
        }
    } else {
        if (discountLine) discountLine.style.display = "none";
        if (discountBadge) discountBadge.style.display = "none";
    }

    let finalAmount = discountedSubtotal;
    const gstCheckbox = document.getElementById("applyGstCheckbox");
    const gstLine = document.getElementById("gstSummaryLine");
    const gstAmountEl = document.getElementById("summaryGstAmount");

    if (gstCheckbox && gstCheckbox.checked && window.BILLING_SETTINGS && window.BILLING_SETTINGS.gstRate) {
        const rate = Number(window.BILLING_SETTINGS.gstRate || 0);
        const gstVal = Math.round((discountedSubtotal * rate / 100) * 100) / 100;
        finalAmount = Math.round((discountedSubtotal + gstVal) * 100) / 100;
        if (gstLine) gstLine.style.display = "flex";
        if (gstAmountEl) gstAmountEl.textContent = `₹${formatMoney(gstVal)}`;
    } else {
        if (gstLine) gstLine.style.display = "none";
    }

    if (grandTotal) {

        grandTotal.textContent =
            `₹${formatMoney(finalAmount)}`;
    }


    // Show footer
    if (cartFooter) {

        cartFooter.style.display =
            "block";
    }


    if (generateButton) {

        generateButton.disabled =
            false;
    }
}

window.updateCartGst = function () {
    renderCart();
};

function syncDiscountInputs() {
    const chk = document.getElementById("enableDiscountCheckbox");
    const typeSelect = document.getElementById("cartDiscountType");
    const valInput = document.getElementById("cartDiscountValue");
    const dp = document.getElementById("cartDiscountPercent");
    const da = document.getElementById("cartDiscountAmount");

    if (!chk || !chk.checked) {
        if (dp) dp.value = "0";
        if (da) da.value = "0";
        return;
    }

    const type = typeSelect ? typeSelect.value : "percent";
    const num = Math.max(0, parseFloat(valInput?.value || 0) || 0);

    if (type === "percent") {
        const pct = Math.min(100, num);
        if (dp) dp.value = pct;
        if (da) da.value = "0";
    } else {
        if (dp) dp.value = "0";
        if (da) da.value = num;
    }
}

window.toggleCartDiscount = function () {
    const chk = document.getElementById("enableDiscountCheckbox");
    const inputArea = document.getElementById("cartDiscountInputArea");
    const valInput = document.getElementById("cartDiscountValue");
    if (!chk) return;

    if (chk.checked) {
        if (inputArea) inputArea.style.display = "block";
        if (valInput) {
            valInput.value = "";
            valInput.placeholder = "0";
            setTimeout(() => { valInput.focus(); }, 50);
        }
    } else {
        if (inputArea) inputArea.style.display = "none";
        if (valInput) valInput.value = "";
    }
    syncDiscountInputs();
    renderCart();
};

window.onCartDiscountTypeChange = function () {
    const typeSelect = document.getElementById("cartDiscountType");
    const unitLabel = document.getElementById("cartDiscountUnitLabel");
    const prefixLabel = document.getElementById("cartDiscountPrefix");
    const valInput = document.getElementById("cartDiscountValue");
    const type = typeSelect ? typeSelect.value : "percent";

    if (type === "percent") {
        if (unitLabel) { unitLabel.textContent = "%"; unitLabel.style.display = "inline"; }
        if (prefixLabel) { prefixLabel.style.display = "none"; }
        if (valInput) {
            valInput.max = "100";
            valInput.placeholder = "0";
            if (parseFloat(valInput.value) > 100) {
                valInput.value = "100";
            }
        }
    } else {
        if (unitLabel) { unitLabel.style.display = "none"; }
        if (prefixLabel) { prefixLabel.textContent = "₹"; prefixLabel.style.display = "inline"; }
        if (valInput) {
            valInput.removeAttribute("max");
            valInput.placeholder = "0";
        }
    }
    if (valInput) valInput.focus();
    syncDiscountInputs();
    renderCart();
};

window.updateCartDiscounts = function () {
    syncDiscountInputs();
    renderCart();
};


// ============================================================
// REMOVE CART ITEM
// ============================================================

function removeCartItem(cartId) {

    cart =
        cart.filter(function (item) {

            return Number(item.id) !==
                Number(cartId);

        });


    renderCart();
}


// ============================================================
// CLEAR CART
// ============================================================

function clearCart() {

    if (!cart.length) {
        return;
    }


    if (
        !confirm(
            "Clear all products from the cart?"
        )
    ) {

        return;
    }


    cart = [];

    const customerField =
        document.getElementById("customerName");

    if (customerField) {
        customerField.value = "";
    }

    const discChk = document.getElementById("enableDiscountCheckbox");
    if (discChk) discChk.checked = false;
    const discArea = document.getElementById("cartDiscountInputArea");
    if (discArea) discArea.style.display = "none";
    const discVal = document.getElementById("cartDiscountValue");
    if (discVal) discVal.value = "";

    const discountPctInput = document.getElementById("cartDiscountPercent");
    if (discountPctInput) discountPctInput.value = "0";

    const discountAmtInput = document.getElementById("cartDiscountAmount");
    if (discountAmtInput) discountAmtInput.value = "0";

    renderCart();
}


// ============================================================
// CLEAR SELECTED PRODUCT
// ============================================================

function clearSelectedProduct() {

    selectedProduct = null;


    const selectedSection =
        document.getElementById(
            "selectedProduct"
        );

    if (selectedSection) {

        selectedSection.style.display =
            "none";

        selectedSection.classList.remove(
            "selected-product-animation"
        );
    }


    const searchInput =
        document.getElementById(
            "productSearch"
        );

    if (searchInput) {

        searchInput.value = "";

        searchInput.focus();
    }


    const searchResults =
        document.getElementById(
            "searchResults"
        );

    if (searchResults) {

        searchResults.innerHTML = "";

        searchResults.classList.remove(
            "active"
        );

        searchResults.style.display =
            "";
    }


    const quantity =
        document.getElementById(
            "quantityValue"
        );

    if (quantity) {
        quantity.value = "";
    }


    const addButton =
        document.getElementById(
            "addToCartButton"
        );

    if (addButton) {
        addButton.disabled = true;
    }


    setText(
        "displayQuantity",
        "0"
    );

    setText(
        "displayUnitPrice",
        "₹0.00"
    );

    setText(
        "displayItemTotal",
        "₹0.00"
    );
}


// ============================================================
// CLEAR SEARCH
// ============================================================

function clearProductSearch() {

    clearSelectedProduct();
}


// ============================================================
// EDIT CART
// ============================================================

function openEditCart(cartId) {

    const item =
        cart.find(function (cartItem) {

            return Number(
                cartItem.id
            ) === Number(cartId);

        });


    if (!item) {

        console.error(
            "Cart item not found:",
            cartId
        );

        return;
    }


    editingCartId =
        item.id;


    const nameEl = document.getElementById("editProductName");
    if (nameEl) {
        nameEl.textContent = item.billingName || item.productName;
    }

    setValue(
        "editCartId",
        item.id
    );


    setValue(
        "editQuantity",
        item.quantity
    );


    setValue(
        "editUnitPrice",
        item.price
    );


    const editUnit =
        document.getElementById(
            "editQuantityUnit"
        );


    if (editUnit) {

        editUnit.innerHTML = "";


        const basis =
            String(
                item.priceBasis ||
                "Piece"
            ).toLowerCase();


        if (basis === "kg") {

            editUnit.innerHTML = `
                <option value="KG">KG</option>
                <option value="Gram">Gram</option>
            `;

        } else if (
            basis === "litre" ||
            basis === "liter"
        ) {

            editUnit.innerHTML = `
                <option value="Litre">Litre</option>
                <option value="ML">ML</option>
            `;

        } else {

            editUnit.innerHTML = `
                <option value="${escapeHtml(
                    item.priceBasis
                )}">
                    ${escapeHtml(
                        item.priceBasis
                    )}
                </option>
            `;
        }


        editUnit.value =
            item.unit;
    }


    const modal =
        document.getElementById(
            "editModal"
        );


    if (modal) {

        modal.style.display =
            "flex";
    }
}


// ============================================================
// SAVE EDITED CART
// ============================================================

function saveEditedCart() {

    if (editingCartId === null) {
        return;
    }


    const item =
        cart.find(function (cartItem) {

            return Number(
                cartItem.id
            ) === Number(
                editingCartId
            );

        });


    if (!item) {

        closeEditModal();

        return;
    }


    const quantityInput =
        document.getElementById(
            "editQuantity"
        );


    const unitInput =
        document.getElementById(
            "editQuantityUnit"
        );


    const priceInput =
        document.getElementById(
            "editUnitPrice"
        );


    const quantity =
        Number(
            quantityInput?.value || 0
        );


    const unit =
        unitInput?.value ||
        item.priceBasis;


    if (
        !Number.isFinite(quantity) ||
        quantity <= 0
    ) {

        alert(
            "Please enter a valid quantity."
        );

        return;
    }


    // Optional price update
    if (priceInput && priceInput.value !== "") {
        const customPrice = parseFloat(priceInput.value);
        if (Number.isFinite(customPrice) && customPrice >= 0) {
            item.price = customPrice;
        }
    }


    // Find product
    const product =
        products.find(function (productItem) {

            return Number(
                productItem.id
            ) === Number(
                item.productId
            );

        });


    if (!product) {

        alert(
            "Product not found."
        );

        return;
    }


    // Convert quantity
    let baseQuantity;


    const basis =
        String(
            product.price_basis
        ).toLowerCase();


    if (
        basis === "kg" &&
        unit.toLowerCase() === "gram"
    ) {

        baseQuantity =
            quantity / 1000;

    } else if (
        (
            basis === "litre" ||
            basis === "liter"
        ) &&
        unit.toLowerCase() === "ml"
    ) {

        baseQuantity =
            quantity / 1000;

    } else {

        baseQuantity =
            quantity;
    }


    // Stock
    if (
        baseQuantity >
        Number(
            product.stock_quantity
        )
    ) {

        alert(
            `Only ${formatQuantity(
                product.stock_quantity
            )} ${
                product.price_basis
            } available.`
        );

        return;
    }


    item.quantity =
        quantity;

    item.unit =
        unit;

    item.baseQuantity =
        baseQuantity;

    item.total =
        baseQuantity *
        Number(
            item.price
        );


    renderCart();

    closeEditModal();
}


// ============================================================
// CLOSE EDIT MODAL
// ============================================================

function closeEditModal() {

    const modal =
        document.getElementById(
            "editModal"
        );


    if (modal) {

        modal.style.display =
            "none";
    }


    editingCartId =
        null;
}


// ============================================================
// GENERATE BILL
// ============================================================

async function generateBill() {

    if (!cart || cart.length === 0) {
        alert("Please add at least one product.");
        return;
    }

    const customerInput =
        document.getElementById("customerName");

    const customerName = customerInput
        ? customerInput.value.trim().replace(/\s+/g, " ")
        : "";

    const mustHaveCustomer =
        !window.BILLING_SETTINGS ||
        window.BILLING_SETTINGS.requireCustomerName;

    if (!customerName && mustHaveCustomer) {
        alert("Please enter the customer name.");

        if (customerInput) {
            customerInput.focus();
        }

        return;
    }

    const button =
        document.getElementById("generateBillButton");

    try {

        if (button) {
            button.disabled = true;
            button.innerHTML = "⏳ Generating...";
        }

        const phoneInput = document.getElementById("customerPhone");
        const customerPhone = phoneInput ? phoneInput.value.trim() : "";
        const gstCheckbox = document.getElementById("applyGstCheckbox");
        const applyGst = gstCheckbox ? gstCheckbox.checked : false;

        const hasProductLevelDiscount = cart.some(function (item) {
            return Number(item.discountPercent || 0) > 0;
        });
        const chk = document.getElementById("enableDiscountCheckbox");
        const isDiscountEnabled = chk && chk.checked && !hasProductLevelDiscount;

        if (typeof syncDiscountInputs === "function") {
            syncDiscountInputs();
        }

        const discountPctInput = document.getElementById("cartDiscountPercent");
        const discountAmtInput = document.getElementById("cartDiscountAmount");
        const cartDiscountPercent = isDiscountEnabled
            ? Math.max(0, Math.min(100, parseFloat(discountPctInput?.value || 0) || 0))
            : 0;
        const flatDiscount = isDiscountEnabled
            ? Math.max(0, parseFloat(discountAmtInput?.value || 0) || 0)
            : 0;

        const subtotal = cart.reduce(function (sum, item) {
            return sum + Number(item.total || 0);
        }, 0);
        const pctDiscountVal = Math.round((subtotal * (cartDiscountPercent / 100)) * 100) / 100;
        const totalDiscount = Math.min(subtotal, Math.round((pctDiscountVal + flatDiscount) * 100) / 100);

        const payload = {
            customer_name: customerName,
            customer_phone: customerPhone,
            apply_gst: applyGst,
            discount_percent: cartDiscountPercent,
            flat_discount: flatDiscount,
            discount_amount: totalDiscount,
            payment_method: (
                document.getElementById("paymentMethod") || {}
            ).value || (
                window.BILLING_SETTINGS
                    ? window.BILLING_SETTINGS.defaultPayment
                    : "Cash"
            ),
            items: cart.map(function (item) {
                return {
                    product_id: item.productId === null ? null : Number(item.productId),
                    batch_id: item.batchId || null,
                    product_name: item.billingName || item.productName,
                    price: item.price,
                    price_basis: item.priceBasis,
                    quantity: Number(item.baseQuantity),
                    quantity_display: Number(item.quantity),
                    quantity_unit: item.unit
                };
            })
        };

        console.log("=================================");
        console.log("SENDING BILL");
        console.log(payload);
        console.log("=================================");


        const response = await fetch("/api/bills", {
            method: "POST",

            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json"
            },

            credentials: "same-origin",

            body: JSON.stringify(payload)
        });


        // Read response as TEXT first.
        // This helps us see exactly what Flask returned.
        const responseText =
            await response.text();

        console.log("HTTP STATUS:", response.status);
        console.log("SERVER RESPONSE:", responseText);


        let result = {};

        try {
            result =
                JSON.parse(responseText);
        } catch (jsonError) {

            console.error(
                "Server did not return JSON:",
                jsonError
            );

            throw new Error(
                "Server returned an invalid response.\n\n" +
                responseText.substring(0, 500)
            );
        }


        console.log(
            "PARSED BILL RESPONSE:",
            result
        );


        // Login expired
        if (response.status === 401) {

            window.location.href = "/";

            return;
        }


        // Flask returned an error
        if (!response.ok) {

            throw new Error(
                result.error ||
                result.message ||
                "Bill generation failed."
            );
        }


        // Accept all common ID formats
        const billId =
            result.bill_id ??
            result.billId ??
            result.id;


        console.log(
            "FINAL BILL ID:",
            billId
        );


        if (
            billId === undefined ||
            billId === null ||
            billId === "" ||
            Number(billId) <= 0
        ) {

            throw new Error(
                "Server successfully responded, but no valid Bill ID was returned.\n\n" +
                "Server response:\n" +
                JSON.stringify(result, null, 2)
            );
        }


        // IMPORTANT:
        // Clear cart only after successful response.
        cart = [];

        if (customerInput) {
            customerInput.value = "";
        }

        const payField = document.getElementById("paymentMethod");

        if (payField) {
            payField.selectedIndex = 0;
        }

        renderCart();


        // Redirect to invoice
        window.location.href =
            `/invoice/${Number(billId)}`;

    } catch (error) {

        console.error(
            "================================="
        );

        console.error(
            "GENERATE BILL ERROR:",
            error
        );

        console.error(
            "================================="
        );


        alert(
            "Unable to generate bill.\n\n" +
            error.message
        );

    } finally {

        if (button) {

            button.disabled =
                cart.length === 0;

            button.innerHTML =
                "Generate Bill";
        }
    }
}


// ============================================================
// HELPERS
// ============================================================

function setText(
    id,
    value
) {

    const element =
        document.getElementById(id);

    if (element) {
        element.textContent =
            value;
    }
}


function setValue(
    id,
    value
) {

    const element =
        document.getElementById(id);

    if (element) {
        element.value =
            value;
    }
}


function formatMoney(value) {

    const number =
        Number(value || 0);

    return number.toLocaleString(
        "en-IN",
        {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }
    );
}


function formatQuantity(value) {

    const number =
        Number(value || 0);


    if (!Number.isFinite(number)) {
        return "0";
    }


    return number.toLocaleString(
        "en-IN",
        {
            maximumFractionDigits: 3
        }
    );
}


function escapeHtml(value) {

    return String(
        value ?? ""
    )
        .replace(
            /&/g,
            "&amp;"
        )
        .replace(
            /</g,
            "&lt;"
        )
        .replace(
            />/g,
            "&gt;"
        )
        .replace(
            /"/g,
            "&quot;"
        )
        .replace(
            /'/g,
            "&#039;"
        );
}


// ============================================================
// ADD PRODUCT TO CART DIRECTLY BY OBJECT (used by barcode scanner)
// ============================================================

function addProductToCartByObj(product) {
    if (!product || !product.id) {
        console.error("addProductToCartByObj: invalid product", product);
        return;
    }

    const stock = Number(product.stock_quantity || 0);
    const rawPrice = Number(product.price || 0);
    const discPct = Number(product.discount_percent || 0);
    const price = discPct > 0
        ? Math.round(rawPrice * (1 - discPct / 100) * 100) / 100
        : rawPrice;
    const priceBasis = product.price_basis || "Piece";

    // Calculate how much is already in cart for this product
    const alreadyInCart = cart
        .filter(function (item) { return Number(item.productId) === Number(product.id); })
        .reduce(function (total, item) { return total + Number(item.baseQuantity || 0); }, 0);

    if (alreadyInCart >= stock) {
        alert("Cannot add more – insufficient stock for: " + (product.billing_name || product.name));
        return;
    }

    // Check if product already in cart → increment by 1
    const existing = cart.find(function (item) {
        return Number(item.productId) === Number(product.id);
    });

    if (existing) {
        existing.baseQuantity = Number(existing.baseQuantity) + 1;
        existing.quantity     = Number(existing.quantity) + 1;
        existing.total        = existing.baseQuantity * Number(existing.price);
    } else {
        cart.push({
            id:          Date.now() + Math.random(),
            productId:   Number(product.id),
            productName: product.name,
            billingName: product.billing_name || product.name,
            quantity:    1,
            unit:        priceBasis,
            baseQuantity: 1,
            price:       price,
            originalPrice: rawPrice,
            discountPercent: discPct,
            priceBasis:  priceBasis,
            total:       price
        });
    }

    console.log("Barcode cart add:", product.billing_name || product.name, "| Cart:", cart);
    renderCart();
}


// ============================================================
// EXPOSE FUNCTIONS TO HTML
// ============================================================

window.searchProducts =
    searchProducts;

window.selectProduct =
    selectProduct;

window.calculateItemTotal =
    calculateItemTotal;

window.addToCart =
    addToCart;

window.addProductToCartByObj =
    addProductToCartByObj;

window.removeCartItem =
    removeCartItem;

window.clearCart =
    clearCart;

window.clearProductSearch =
    clearProductSearch;

window.clearSelectedProduct =
    clearSelectedProduct;

window.openEditCart =
    openEditCart;

window.saveEditedCart =
    saveEditedCart;

// Alias used in billing.html button onclick
window.saveEditedQuantity =
    saveEditedCart;

window.closeEditModal =
    closeEditModal;

window.generateBill =
    generateBill;

// ============================================================
// CUSTOM ITEMS
// ============================================================

function openCustomItemModal() {
    const modal = document.getElementById("customItemModal");
    if (modal) {
        modal.style.display = "flex";
        const nameInput = document.getElementById("customItemName");
        if (nameInput) {
            nameInput.value = "";
            nameInput.focus();
        }
        document.getElementById("customItemPrice").value = "";
        document.getElementById("customItemQuantity").value = "1";
        document.getElementById("customItemUnit").value = "Piece";
    }
}

function closeCustomItemModal() {
    const modal = document.getElementById("customItemModal");
    if (modal) {
        modal.style.display = "none";
    }
}

function addCustomItemToCart() {
    const name = document.getElementById("customItemName").value.trim();
    const priceStr = document.getElementById("customItemPrice").value;
    const quantityStr = document.getElementById("customItemQuantity").value;
    const unit = document.getElementById("customItemUnit").value;
    
    if (!name) {
        alert("Please enter a custom item name.");
        return;
    }
    
    const price = Number(priceStr);
    const quantity = Number(quantityStr);
    
    if (!Number.isFinite(price) || price < 0) {
        alert("Please enter a valid price.");
        return;
    }
    
    if (!Number.isFinite(quantity) || quantity <= 0) {
        alert("Please enter a valid quantity.");
        return;
    }
    
    const total = price * quantity;
    
    // Add to cart with null productId
    cart.push({
        id: Date.now() + Math.random(),
        productId: null,
        productName: name,
        billingName: name,
        quantity: quantity,
        unit: unit,
        baseQuantity: quantity,
        price: price,
        priceBasis: unit,
        total: total
    });
    
    console.log("Cart:", cart);
    
    renderCart();
    closeCustomItemModal();
}

window.openCustomItemModal = openCustomItemModal;
window.closeCustomItemModal = closeCustomItemModal;
window.addCustomItemToCart = addCustomItemToCart;
window.applyBatchChoice = applyBatchChoice;
window.closeBatchTransitionModal = closeBatchTransitionModal;
window.matchBatch1Price = matchBatch1Price;