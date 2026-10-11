# A WinForms test app with the common desktop controls, a paged data grid, a details-view
# file list and a paged set of product "cards". Used by tests/live/run_live_desktop.sh.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Desktop Controls Test"
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(80, 60)
$form.ClientSize = New-Object System.Drawing.Size(900, 640)

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = "Fill"
$form.Controls.Add($tabs)
function New-Tab([string]$name) { $t = New-Object System.Windows.Forms.TabPage; $t.Text = $name; [void]$tabs.TabPages.Add($t); $t }
function Add-Label($parent, [string]$text, [int]$x, [int]$y) {
    $l = New-Object System.Windows.Forms.Label; $l.Text = $text; $l.AutoSize = $true
    $l.Location = New-Object System.Drawing.Point($x, ($y + 3)); $parent.Controls.Add($l)
}

# ---- Form tab: every common control ----
$t1 = New-Tab "Profile"
$y = 16
Add-Label $t1 "Full name" 16 $y
$name = New-Object System.Windows.Forms.TextBox; $name.Name = "txtName"; $name.AccessibleName = "Full name"
$name.Location = New-Object System.Drawing.Point(160, $y); $name.Width = 240; $t1.Controls.Add($name); $y += 34
Add-Label $t1 "Password" 16 $y
$pw = New-Object System.Windows.Forms.TextBox; $pw.Name = "txtPassword"; $pw.AccessibleName = "Password"; $pw.UseSystemPasswordChar = $true
$pw.Location = New-Object System.Drawing.Point(160, $y); $pw.Width = 240; $t1.Controls.Add($pw); $y += 34
Add-Label $t1 "Age" 16 $y
$age = New-Object System.Windows.Forms.NumericUpDown; $age.Name = "numAge"; $age.AccessibleName = "Age"; $age.Maximum = 120
$age.Location = New-Object System.Drawing.Point(160, $y); $t1.Controls.Add($age); $y += 34
Add-Label $t1 "Date of birth" 16 $y
$dob = New-Object System.Windows.Forms.DateTimePicker; $dob.Name = "dtpDob"; $dob.AccessibleName = "Date of birth"
$dob.Format = "Custom"; $dob.CustomFormat = "yyyy-MM-dd"; $dob.Value = [datetime]"2000-01-01"
$dob.Location = New-Object System.Drawing.Point(160, $y); $t1.Controls.Add($dob); $y += 34
Add-Label $t1 "Country" 16 $y
$country = New-Object System.Windows.Forms.ComboBox; $country.Name = "cmbCountry"; $country.AccessibleName = "Country"; $country.DropDownStyle = "DropDownList"
[void]$country.Items.AddRange(@("Choose...", "India", "Japan", "Singapore")); $country.SelectedIndex = 0
$country.Location = New-Object System.Drawing.Point(160, $y); $country.Width = 200; $t1.Controls.Add($country); $y += 34
Add-Label $t1 "City" 16 $y
$city = New-Object System.Windows.Forms.ComboBox; $city.Name = "cmbCity"; $city.AccessibleName = "City"; $city.DropDownStyle = "DropDown"
[void]$city.Items.AddRange(@("Chennai", "Tokyo", "Singapore"))
$city.Location = New-Object System.Drawing.Point(160, $y); $city.Width = 200; $t1.Controls.Add($city); $y += 34
Add-Label $t1 "Languages" 16 $y
$langs = New-Object System.Windows.Forms.ListBox; $langs.Name = "lstLanguages"; $langs.AccessibleName = "Languages"; $langs.SelectionMode = "MultiExtended"
[void]$langs.Items.AddRange(@("English", "Hindi", "Japanese", "Tamil")); $langs.Height = 70
$langs.Location = New-Object System.Drawing.Point(160, $y); $langs.Width = 200; $t1.Controls.Add($langs); $y += 80
$grp = New-Object System.Windows.Forms.GroupBox; $grp.Text = "Plan"; $grp.Location = New-Object System.Drawing.Point(16, $y); $grp.Size = New-Object System.Drawing.Size(384, 50)
$plans = @{}
$x = 12
foreach ($p in @("Free", "Pro", "Team")) {
    $r = New-Object System.Windows.Forms.RadioButton; $r.Text = $p; $r.Name = "rad$p"; $r.AutoSize = $true
    $r.Location = New-Object System.Drawing.Point($x, 20); $grp.Controls.Add($r); $plans[$p] = $r; $x += 110
}
$plans["Free"].Checked = $true
$t1.Controls.Add($grp); $y += 60
$news = New-Object System.Windows.Forms.CheckBox; $news.Name = "chkNews"; $news.Text = "Send me the newsletter"; $news.AutoSize = $true
$news.Location = New-Object System.Drawing.Point(16, $y); $t1.Controls.Add($news); $y += 32
Add-Label $t1 "Volume" 16 $y
$vol = New-Object System.Windows.Forms.TrackBar; $vol.Name = "trkVolume"; $vol.AccessibleName = "Volume"; $vol.Minimum = 0; $vol.Maximum = 100; $vol.Value = 20; $vol.TickFrequency = 10
$vol.Location = New-Object System.Drawing.Point(160, $y); $vol.Width = 240; $t1.Controls.Add($vol); $y += 50
$submit = New-Object System.Windows.Forms.Button; $submit.Name = "btnSubmit"; $submit.Text = "Submit profile"; $submit.Width = 140
$submit.Location = New-Object System.Drawing.Point(16, $y); $t1.Controls.Add($submit)
$result = New-Object System.Windows.Forms.Label; $result.Name = "lblResult"; $result.Text = "Not submitted"; $result.AutoSize = $true
$result.Location = New-Object System.Drawing.Point(170, ($y + 5)); $t1.Controls.Add($result)
$submit.Add_Click({
    $plan = ($plans.GetEnumerator() | Where-Object { $_.Value.Checked } | Select-Object -First 1).Key
    $chosen = ($langs.SelectedItems | ForEach-Object { $_ }) -join "+"
    $result.Text = "Submitted | $($name.Text) | $($age.Value) | $($dob.Value.ToString('yyyy-MM-dd')) | $($country.SelectedItem) | $($city.Text) | $chosen | $plan | " +
        $(if ($news.Checked) { "news" } else { "no-news" }) + " | vol=$($vol.Value) | pw=$($pw.Text.Length)"
})

# ---- Orders tab: a data grid, 10 rows a page ----
$t2 = New-Tab "Orders"
$grid = New-Object System.Windows.Forms.DataGridView; $grid.Name = "gridOrders"; $grid.AccessibleName = "Orders"
$grid.Location = New-Object System.Drawing.Point(12, 12); $grid.Size = New-Object System.Drawing.Size(620, 300)
$grid.AllowUserToAddRows = $false; $grid.ReadOnly = $true; $grid.RowHeadersVisible = $false
foreach ($c in @("Order", "Customer", "Amount", "Status")) { [void]$grid.Columns.Add($c, $c) }
$t2.Controls.Add($grid)
$customers = @("Asha", "Bilal", "Chen", "Divya", "Emi", "Farah", "Gopal"); $statuses = @("Paid", "Pending", "Shipped")
$orderCount = 23
$script:page = 0
$prev = New-Object System.Windows.Forms.Button; $prev.Text = "Previous"; $prev.Name = "btnPrev"; $prev.Location = New-Object System.Drawing.Point(12, 322)
$next = New-Object System.Windows.Forms.Button; $next.Text = "Next"; $next.Name = "btnNext"; $next.Location = New-Object System.Drawing.Point(220, 322)
$pageInfo = New-Object System.Windows.Forms.Label; $pageInfo.AutoSize = $true; $pageInfo.Location = New-Object System.Drawing.Point(100, 327)
$t2.Controls.AddRange(@($prev, $next, $pageInfo))
function Show-Orders {
    $grid.Rows.Clear()
    for ($i = $script:page * 10; $i -lt [math]::Min($orderCount, $script:page * 10 + 10); $i++) {
        [void]$grid.Rows.Add("ORD-$(1001 + $i)", $customers[$i % 7], ("{0:F2}" -f (120 + $i * 37)), $statuses[$i % 3])
    }
    $pages = [math]::Ceiling($orderCount / 10)
    $pageInfo.Text = "Page $($script:page + 1) of $pages"
    $prev.Enabled = $script:page -gt 0; $next.Enabled = $script:page -lt $pages - 1
}
$prev.Add_Click({ $script:page--; Show-Orders }); $next.Add_Click({ $script:page++; Show-Orders })
Show-Orders

# ---- Files tab: a details-view list ----
$t3 = New-Tab "Files"
$files = New-Object System.Windows.Forms.ListView; $files.Name = "lvFiles"; $files.AccessibleName = "Files"; $files.View = "Details"; $files.FullRowSelect = $true
$files.Location = New-Object System.Drawing.Point(12, 12); $files.Size = New-Object System.Drawing.Size(620, 300)
foreach ($c in @(@("Name", 220), @("Size", 100), @("Type", 140))) { [void]$files.Columns.Add($c[0], $c[1]) }
for ($i = 1; $i -le 30; $i++) {
    $item = New-Object System.Windows.Forms.ListViewItem("report_$('{0:D2}' -f $i).pdf")
    [void]$item.SubItems.Add("$($i * 12) KB"); [void]$item.SubItems.Add("PDF document"); [void]$files.Items.Add($item)
}
$t3.Controls.Add($files)

# ---- Products tab: cards, 6 a page ----
$t4 = New-Tab "Products"
$flow = New-Object System.Windows.Forms.FlowLayoutPanel; $flow.Name = "flowProducts"; $flow.AccessibleName = "Products"
$flow.Location = New-Object System.Drawing.Point(12, 12); $flow.Size = New-Object System.Drawing.Size(860, 420)
$t4.Controls.Add($flow)
$productNames = @("Wireless Mouse", "USB-C Hub", "Mechanical Keyboard", "27 inch Monitor", "Laptop Stand", "Headphones",
                  "Webcam 1080p", "Portable SSD", "Desk Lamp", "Ergonomic Chair", "HDMI Cable", "Phone Holder", "Bluetooth Speaker", "Smart Watch")
$script:ppage = 0
$pprev = New-Object System.Windows.Forms.Button; $pprev.Text = "Previous page"; $pprev.Width = 110; $pprev.Location = New-Object System.Drawing.Point(12, 444)
$pnext = New-Object System.Windows.Forms.Button; $pnext.Text = "Next page"; $pnext.Width = 110; $pnext.Location = New-Object System.Drawing.Point(240, 444)
$ppageInfo = New-Object System.Windows.Forms.Label; $ppageInfo.AutoSize = $true; $ppageInfo.Location = New-Object System.Drawing.Point(130, 449)
$t4.Controls.AddRange(@($pprev, $pnext, $ppageInfo))
function Show-Products {
    $flow.Controls.Clear()
    for ($i = $script:ppage * 6; $i -lt [math]::Min($productNames.Count, $script:ppage * 6 + 6); $i++) {
        $card = New-Object System.Windows.Forms.Panel; $card.Size = New-Object System.Drawing.Size(260, 120); $card.BorderStyle = "FixedSingle"
        $card.AccessibleName = $productNames[$i]
        $title = New-Object System.Windows.Forms.Label; $title.Text = $productNames[$i]; $title.AutoSize = $true; $title.Location = New-Object System.Drawing.Point(8, 8)
        $price = New-Object System.Windows.Forms.Label; $price.Text = "Rs. $('{0:N0}' -f (499 + $i * 350))"; $price.AutoSize = $true; $price.Location = New-Object System.Drawing.Point(8, 32)
        $rating = New-Object System.Windows.Forms.Label; $rating.Text = "$('{0:F1}' -f (3.6 + ($i % 5) * 0.3)) out of 5 stars"; $rating.AutoSize = $true; $rating.Location = New-Object System.Drawing.Point(8, 56)
        $buy = New-Object System.Windows.Forms.Button; $buy.Text = "Add to cart"; $buy.Width = 100; $buy.Location = New-Object System.Drawing.Point(8, 82)
        $card.Controls.AddRange(@($title, $price, $rating, $buy)); $flow.Controls.Add($card)
    }
    $pages = [math]::Ceiling($productNames.Count / 6)
    $ppageInfo.Text = "Page $($script:ppage + 1) of $pages"
    $pprev.Enabled = $script:ppage -gt 0; $pnext.Enabled = $script:ppage -lt $pages - 1
}
$pprev.Add_Click({ $script:ppage--; Show-Products }); $pnext.Add_Click({ $script:ppage++; Show-Products })
Show-Products

[void]$form.ShowDialog()
