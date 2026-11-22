# Throwaway WinForms window used by selftest.py.
# It exposes stable, English-named UI Automation controls (AutomationId = Name)
# so the self-test never depends on localized system dialogs such as Run.
param(
    [string]$Title = "Computer-use UIA selftest",
    [string]$StateFile = ""
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Set-State {
    param([string]$Text)
    if ($StateFile) {
        try {
            [IO.File]::WriteAllText($StateFile, $Text)
        } catch { }
    }
}

[Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object Windows.Forms.Form
$form.Text = $Title
$form.Name = "TestForm"
$form.ClientSize = New-Object Drawing.Size(780, 500)
$form.StartPosition = "Manual"
$form.Location = New-Object Drawing.Point(80, 80)
$form.TopMost = $true

$menu = New-Object Windows.Forms.MenuStrip
$menu.Name = "TestMenu"
$fileMenu = New-Object Windows.Forms.ToolStripMenuItem("File")
$fileMenu.Name = "FileMenu"
$testAction = New-Object Windows.Forms.ToolStripMenuItem("Test Action")
$testAction.Name = "TestAction"
$testAction.Add_Click({ Set-State "menu-clicked" })
[void]$fileMenu.DropDownItems.Add($testAction)
[void]$menu.Items.Add($fileMenu)
$form.MainMenuStrip = $menu
$form.Controls.Add($menu)

$label = New-Object Windows.Forms.Label
$label.Name = "StatusLabel"
$label.Text = "Ready"
$label.AutoSize = $true
$label.Location = New-Object Drawing.Point(20, 42)
$form.Controls.Add($label)

$field = New-Object Windows.Forms.TextBox
$field.Name = "TestField"
$field.Text = "initial"
$field.Location = New-Object Drawing.Point(20, 70)
$field.Width = 220
$form.Controls.Add($field)

$button = New-Object Windows.Forms.Button
$button.Name = "TestButton"
$button.Text = "Test Button"
$button.Location = New-Object Drawing.Point(270, 68)
$button.Width = 110
$button.Add_Click({
    $label.Text = "Clicked"
    Set-State "button-clicked"
})
$form.Controls.Add($button)

$check = New-Object Windows.Forms.CheckBox
$check.Name = "TestCheck"
$check.Text = "Test Check"
$check.AutoSize = $true
$check.Location = New-Object Drawing.Point(20, 108)
$check.Add_CheckedChanged({ Set-State ("check:" + $check.Checked) })
$form.Controls.Add($check)

$combo = New-Object Windows.Forms.ComboBox
$combo.Name = "TestCombo"
$combo.DropDownStyle = [Windows.Forms.ComboBoxStyle]::DropDownList
$combo.Location = New-Object Drawing.Point(20, 140)
$combo.Width = 220
[void]$combo.Items.AddRange(@("Alpha", "Beta", "Gamma"))
$combo.SelectedIndex = 0
$combo.Add_SelectedIndexChanged({ Set-State ("combo:" + $combo.SelectedItem) })
$form.Controls.Add($combo)

$list = New-Object Windows.Forms.ListBox
$list.Name = "TestList"
$list.Location = New-Object Drawing.Point(20, 180)
$list.Size = New-Object Drawing.Size(240, 260)
[void]$list.Items.AddRange(@("Row One", "Row Two", "Row Three"))
for ($index = 1; $index -le 60; $index++) {
    [void]$list.Items.Add("Item $index")
}
$form.Controls.Add($list)

$grid = New-Object Windows.Forms.DataGridView
$grid.Name = "TestGrid"
$grid.Location = New-Object Drawing.Point(400, 68)
$grid.Size = New-Object Drawing.Size(350, 260)
$grid.AllowUserToAddRows = $false
$grid.AllowUserToDeleteRows = $false
$grid.RowHeadersVisible = $false
$grid.ReadOnly = $true
$grid.AutoSizeColumnsMode = [Windows.Forms.DataGridViewAutoSizeColumnsMode]::Fill
$colName = New-Object Windows.Forms.DataGridViewTextBoxColumn
$colName.Name = "Name"
$colName.HeaderText = "Name"
$colValue = New-Object Windows.Forms.DataGridViewTextBoxColumn
$colValue.Name = "Value"
$colValue.HeaderText = "Value"
[void]$grid.Columns.Add($colName)
[void]$grid.Columns.Add($colValue)
[void]$grid.Rows.Add("Alpha", "1")
[void]$grid.Rows.Add("Beta", "2")
[void]$grid.Rows.Add("Gamma", "3")
$form.Controls.Add($grid)

$hide = New-Object Windows.Forms.Button
$hide.Name = "HideMe"
$hide.Text = "Hide Me"
$hide.Location = New-Object Drawing.Point(270, 108)
$hide.Width = 110
$hide.Add_Click({
    $hide.Visible = $false
    Set-State "hidden"
})
$form.Controls.Add($hide)

$openButton = New-Object Windows.Forms.Button
$openButton.Name = "OpenDialog"
$openButton.Text = "Open Dialog"
$openButton.Location = New-Object Drawing.Point(270, 140)
$openButton.Width = 110
$openButton.Add_Click({
    $dialog = New-Object Windows.Forms.OpenFileDialog
    $dialog.Title = "Computer-use file dialog"
    $dialog.CheckFileExists = $false
    $dialog.CheckPathExists = $false
    $dialog.InitialDirectory = [IO.Path]::GetTempPath()
    if ($dialog.ShowDialog($form) -eq [Windows.Forms.DialogResult]::OK) {
        Set-State ("file:" + $dialog.FileName)
    }
})
$form.Controls.Add($openButton)

$ticker = New-Object Windows.Forms.Label
$ticker.Name = "TickerLabel"
$ticker.Text = "tick 0"
$ticker.AutoSize = $true
$ticker.Location = New-Object Drawing.Point(300, 345)
$form.Controls.Add($ticker)

$script:ticks = 0
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 450
$timer.Add_Tick({
    $script:ticks++
    if (($script:ticks % 2) -eq 0) {
        $ticker.Text = "tick even"
    } else {
        $ticker.Text = "tick odd"
    }
})
$timer.Start()

[void]$form.ShowDialog()
