import io
import time
import pandas as pd
import streamlit as st
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

st.set_page_config(page_title="AI Invoice & GST Autopilot", page_icon="⚡", layout="wide")

# --- SIDEBAR: SETTINGS & API PROTECTION ---
with st.sidebar:
    st.header("⚙️ Autopilot Settings")
    region_mode = st.radio(
        "Accounting Format",
        ["India GST (Tally / Zoho)", "Global USD/EUR (QuickBooks / Xero)"],
        index=0,
    )
    st.divider()
    user_api_key = st.text_input(
        "Custom Gemini API Key (Optional)",
        type="password",
        help="Leave blank to use default server key, or paste your own free Google AI Studio key for unlimited batches.",
    )
    st.caption("Built for zero-error bulk accounting & automated math verification.")

# Resolve API Key (User BYOK takes priority over server secret)
active_api_key = user_api_key.strip() if user_api_key.strip() else st.secrets.get("GEMINI_API_KEY", "")

if not active_api_key:
    st.error("Missing Gemini API Key! Paste one in the left sidebar or add GEMINI_API_KEY in Streamlit Secrets.")
    st.stop()

client = genai.Client(api_key=active_api_key)

# --- DATA SCHEMAS ---
class LineItem(BaseModel):
    item_name: str = Field(description="Product or service description")
    hsn_sac: str = Field(default="", description="HSN or SAC code if shown")
    quantity: float = Field(default=1.0, description="Quantity purchased")
    unit: str = Field(default="Nos", description="Unit of measure e.g. KG, Piece, Box, Nos")
    unit_rate: float = Field(default=0.0, description="Price per unit before tax")
    tax_percent: float = Field(default=0.0, description="GST or tax percentage on this item")
    line_total: float = Field(description="Total amount for this line row")


class InvoiceSchema(BaseModel):
    vendor_name: str = Field(description="Seller or supplier business name")
    vendor_gstin: str = Field(default="", description="Seller GSTIN or Tax ID if shown")
    buyer_name: str = Field(default="", description="Bill-to customer or buyer name")
    buyer_gstin: str = Field(default="", description="Bill-to customer GSTIN or Tax ID if shown")
    place_of_supply: str = Field(default="", description="State or region of supply if shown")
    invoice_number: str = Field(description="Invoice or bill number")
    invoice_date: str = Field(description="Invoice date")
    due_date: str = Field(default="", description="Payment due date if shown")
    currency: str = Field(default="INR", description="Currency code e.g. INR, USD, EUR, GBP")
    taxable_amount: float = Field(default=0.0, description="Pre-tax taxable subtotal")
    cgst_amount: float = Field(default=0.0, description="CGST amount if applicable")
    sgst_amount: float = Field(default=0.0, description="SGST amount if applicable")
    igst_amount: float = Field(default=0.0, description="IGST or sales tax/VAT amount")
    discount_amount: float = Field(default=0.0, description="Discount deducted if any")
    grand_total: float = Field(description="Final payable grand total")
    line_items: list[LineItem] = Field(description="All individual product/item rows on the invoice")


def prepare_document(uploaded_file):
    """Prepares native PDF bytes or downscales large images for fast ~2s extraction."""
    file_bytes = uploaded_file.getvalue()
    if uploaded_file.type == "application/pdf":
        return types.Part.from_bytes(data=file_bytes, mime_type="application/pdf")

    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    img.thumbnail((1400, 1400))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


def extract_with_retry(doc_part, prompt, max_retries=3):
    """Calls Gemini with automatic backoff if free-tier rate limits are hit during large batches."""
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[prompt, doc_part],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=InvoiceSchema,
                    temperature=0.0,
                ),
            )
            return response.parsed
        except Exception as e:
            err_msg = str(e).lower()
            if ("429" in err_msg or "quota" in err_msg or "resource_exhausted" in err_msg) and attempt < max_retries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise e


def verify_invoice_math(inv: InvoiceSchema):
    """Checks line items, taxable subtotal, and GST/tax totals against Grand Total."""
    items_sum = round(sum(item.line_total for item in inv.line_items), 2)
    tax_sum = round(inv.cgst_amount + inv.sgst_amount + inv.igst_amount, 2)

    # Case 1: Line totals already include tax (common in Indian retail/wholesale bills)
    if abs(inv.grand_total - items_sum) <= 1.0:
        return True, "✅ VERIFIED (Items match Total)", items_sum
    # Case 2: Line totals are pre-tax + tax - discount == grand total
    if abs((items_sum + tax_sum - inv.discount_amount) - inv.grand_total) <= 1.0:
        return True, "✅ VERIFIED (Items + Tax match Total)", items_sum
    # Case 3: Taxable subtotal + tax - discount == grand total
    if inv.taxable_amount > 0 and abs((inv.taxable_amount + tax_sum - inv.discount_amount) - inv.grand_total) <= 1.0:
        return True, "✅ VERIFIED (Taxable + Tax match Total)", items_sum

    diff = round(inv.grand_total - items_sum, 2)
    return False, f"⚠️ REVIEW NEEDED (Diff: {diff})", items_sum


# --- MAIN UI ---
st.title("⚡ AI Batch Invoice & GST Autopilot")
st.markdown("Upload **1 to 50+ invoices** (PDF, PNG, JPG) at once to generate a single, math-verified Excel workbook.")

uploaded_files = st.file_uploader(
    "Drop Bulk Invoices Here",
    type=["pdf", "png", "jpg", "jpeg"],
    accept_multiple_files=True,
)

if uploaded_files:
    st.info(f"📁 **{len(uploaded_files)} file(s)** queued for batch extraction.")

    if st.button(f"🚀 Process All {len(uploaded_files)} Invoice(s)", type="primary", use_container_width=True):
        batch_start = time.time()
        progress_bar = st.progress(0)
        status_text = st.empty()

        all_summaries = []
        all_items = []
        exceptions = []

        prompt = (
            f"Extract all invoice header details, tax breakdowns ({region_mode}), and every individual line item. "
            "Keep seller GSTIN separate from buyer GSTIN. Return pure numeric floats for all amounts."
        )

        for idx, file in enumerate(uploaded_files):
            status_text.text(f"⏳ Processing ({idx + 1}/{len(uploaded_files)}): {file.name}...")
            try:
                doc_part = prepare_document(file)
                inv = extract_with_retry(doc_part, prompt)
                is_valid, audit_status, items_sum = verify_invoice_math(inv)

                summary_row = {
                    "Source File": file.name,
                    "Vendor Name": inv.vendor_name,
                    "Vendor GSTIN": inv.vendor_gstin,
                    "Buyer Name": inv.buyer_name,
                    "Buyer GSTIN": inv.buyer_gstin,
                    "Place of Supply": inv.place_of_supply,
                    "Invoice No": inv.invoice_number,
                    "Invoice Date": inv.invoice_date,
                    "Due Date": inv.due_date,
                    "Currency": inv.currency,
                    "Taxable Amount": inv.taxable_amount,
                    "CGST": inv.cgst_amount,
                    "SGST": inv.sgst_amount,
                    "IGST / Tax": inv.igst_amount,
                    "Discount": inv.discount_amount,
                    "Grand Total": inv.grand_total,
                    "Math Audit": audit_status,
                }
                all_summaries.append(summary_row)

                if not is_valid:
                    exceptions.append(summary_row)

                for item in inv.line_items:
                    all_items.append({
                        "Source File": file.name,
                        "Invoice No": inv.invoice_number,
                        "Invoice Date": inv.invoice_date,
                        "Vendor Name": inv.vendor_name,
                        "Item Name": item.item_name,
                        "HSN/SAC": item.hsn_sac,
                        "Qty": item.quantity,
                        "Unit": item.unit,
                        "Rate": item.unit_rate,
                        "Tax %": item.tax_percent,
                        "Line Total": item.line_total,
                    })

                # Gentle pacing on multi-file batches to stay smooth on free tier
                if len(uploaded_files) > 3 and idx < len(uploaded_files) - 1:
                    time.sleep(0.5)

            except Exception as e:
                err_row = {
                    "Source File": file.name,
                    "Vendor Name": "READ ERROR",
                    "Invoice No": "-",
                    "Grand Total": 0.0,
                    "Math Audit": f"❌ ERROR: {str(e)[:80]}",
                }
                all_summaries.append(err_row)
                exceptions.append(err_row)

            progress_bar.progress((idx + 1) / len(uploaded_files))

        total_time = round(time.time() - batch_start, 2)
        status_text.empty()

        summary_df = pd.DataFrame(all_summaries)
        items_df = pd.DataFrame(all_items)
        exceptions_df = pd.DataFrame(exceptions) if exceptions else pd.DataFrame([{"Status": "Zero errors — all invoices verified!"}])

        verified_count = sum(1 for r in all_summaries if "✅" in str(r.get("Math Audit", "")))
        total_value = pd.to_numeric(summary_df["Grand Total"], errors="coerce").fillna(0).sum()

        st.success(f"🎉 Batch Complete in **{total_time}s**! ({verified_count}/{len(uploaded_files)} Verified)")

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Invoices Processed", len(uploaded_files))
        k2.metric("Line Items Extracted", len(items_df))
        k3.metric("Total Batch Value", f"{total_value:,.2f}")
        k4.metric("Needs Review", len(exceptions))

        tab1, tab2, tab3 = st.tabs(["📦 Combined Line Items", "🧾 Master Invoice Summary", "⚠️ Review Exceptions"])
        with tab1:
            st.dataframe(items_df, use_container_width=True, hide_index=True)
        with tab2:
            st.dataframe(summary_df, use_container_width=True, hide_index=True)
        with tab3:
            st.dataframe(exceptions_df, use_container_width=True, hide_index=True)

        # Build 3-Sheet Combined Excel File
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            items_df.to_excel(writer, index=False, sheet_name="All_Line_Items")
            summary_df.to_excel(writer, index=False, sheet_name="Invoices_Master_Summary")
            exceptions_df.to_excel(writer, index=False, sheet_name="Review_Exceptions")

        st.download_button(
            label=f"📥 Download Combined Excel ({len(uploaded_files)} Invoices .xlsx)",
            data=excel_buffer.getvalue(),
            file_name=f"Batch_Invoices_{len(uploaded_files)}_Files.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )