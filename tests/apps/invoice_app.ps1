# A small WinForms invoice entry app used as a test and demo target.
# -Variant v2 renames and moves controls, to simulate a UI change that breaks a recorded skill.
param([string]$Variant = "v1", [int]$PoLimit = 5000)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$v2 = $Variant -eq "v2"
$form = New-Object System.Windows.Forms.Form
$form.Text = "Invoice Entry - Acme ERP"
$form.Name = "InvoiceForm"
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(120, 120)
$form.ClientSize = New-Object System.Drawing.Size(520, 420)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false

function Add-Label([string]$text, [int]$x, [int]$y) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.AutoSize = $true
    $l.Location = New-Object System.Drawing.Point($x, ($y + 3))
    $form.Controls.Add($l)
}

$rowInvoice = 20; $rowVendor = 60; $rowAmount = 100
if ($v2) { $rowVendor = 20; $rowInvoice = 60 }   # v2 swaps two rows

Add-Label $(if ($v2) { "Invoice number" } else { "Invoice No" }) 20 $rowInvoice
$txtInvoice = New-Object System.Windows.Forms.TextBox
$txtInvoice.Name = $(if ($v2) { "invoiceNumberBox" } else { "txtInvoiceNo" })
$txtInvoice.AccessibleName = $(if ($v2) { "Invoice number" } else { "Invoice No" })
$txtInvoice.Location = New-Object System.Drawing.Point(140, $rowInvoice); $txtInvoice.Width = 200
$form.Controls.Add($txtInvoice)

Add-Label "Vendor" 20 $rowVendor
$cmbVendor = New-Object System.Windows.Forms.ComboBox
$cmbVendor.Name = "cmbVendor"; $cmbVendor.AccessibleName = "Vendor"
$cmbVendor.DropDownStyle = "DropDownList"
[void]$cmbVendor.Items.AddRange(@("Select...", "Northwind Paper", "Contoso Supplies", "Fabrikam Tools"))
$cmbVendor.SelectedIndex = 0
$cmbVendor.Location = New-Object System.Drawing.Point(140, $rowVendor); $cmbVendor.Width = 200
$form.Controls.Add($cmbVendor)

Add-Label "Amount" 20 $rowAmount
$txtAmount = New-Object System.Windows.Forms.TextBox
$txtAmount.Name = "txtAmount"; $txtAmount.AccessibleName = "Amount"
$txtAmount.Location = New-Object System.Drawing.Point(140, $rowAmount); $txtAmount.Width = 200
$form.Controls.Add($txtAmount)

$chkTax = New-Object System.Windows.Forms.CheckBox
$chkTax.Name = "chkTax"; $chkTax.Text = "Tax inclusive"; $chkTax.AutoSize = $true
$chkTax.Location = New-Object System.Drawing.Point(140, 138)
$form.Controls.Add($chkTax)

$btnSave = New-Object System.Windows.Forms.Button
$btnSave.Name = $(if ($v2) { "submitButton" } else { "btnSave" })
$btnSave.Text = $(if ($v2) { "Submit" } else { "Save" })
$btnSave.Location = $(if ($v2) { New-Object System.Drawing.Point(380, 170) } else { New-Object System.Drawing.Point(140, 170) })
$btnSave.Width = 90
$form.Controls.Add($btnSave)

$btnClear = New-Object System.Windows.Forms.Button
$btnClear.Name = "btnClear"; $btnClear.Text = "Clear"
$btnClear.Location = New-Object System.Drawing.Point(250, 170); $btnClear.Width = 90
$form.Controls.Add($btnClear)

$status = New-Object System.Windows.Forms.Label
$status.Name = "lblStatus"; $status.Text = "Ready"; $status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(20, 215)
$form.Controls.Add($status)

$list = New-Object System.Windows.Forms.ListBox
$list.Name = "lstSaved"; $list.AccessibleName = "Saved invoices"
$list.Location = New-Object System.Drawing.Point(20, 245); $list.Size = New-Object System.Drawing.Size(480, 160)
$form.Controls.Add($list)

$btnClear.Add_Click({
    $txtInvoice.Text = ""; $txtAmount.Text = ""; $cmbVendor.SelectedIndex = 0; $chkTax.Checked = $false
    $status.Text = "Ready"
})

$btnSave.Add_Click({
    $amount = 0.0
    if ([string]::IsNullOrWhiteSpace($txtInvoice.Text)) { $status.Text = "Error: invoice number is required"; return }
    if ($cmbVendor.SelectedIndex -le 0) { $status.Text = "Error: choose a vendor"; return }
    if (-not [double]::TryParse($txtAmount.Text, [ref]$amount)) { $status.Text = "Error: amount must be a number"; return }
    if ($amount -gt $PoLimit) { $status.Text = "Blocked: amount $amount exceeds purchase order limit $PoLimit"; return }
    $tax = $(if ($chkTax.Checked) { "tax incl." } else { "tax excl." })
    [void]$list.Items.Add("$($txtInvoice.Text) | $($cmbVendor.SelectedItem) | $amount | $tax")
    $status.Text = "Saved $($txtInvoice.Text)"
    $txtInvoice.Text = ""; $txtAmount.Text = ""; $cmbVendor.SelectedIndex = 0; $chkTax.Checked = $false
})

[void]$form.ShowDialog()
