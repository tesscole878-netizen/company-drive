using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;

namespace CompanyDrive
{
    internal sealed class FileChangePopup : Form
    {
        readonly Panel content = new Panel { AutoScroll = true };
        readonly Label title = new Label { Text = "苏盐新云盘 · 文件更新", TextAlign = ContentAlignment.MiddleLeft };
        readonly Button close = new Button { Text = "×", AccessibleName = "关闭提醒", FlatStyle = FlatStyle.Flat,
            TabStop = false, Cursor = Cursors.Hand };
        readonly Timer dismissTimer = new Timer { Interval = 15000 };
        readonly List<Dictionary<string, object>> changes = new List<Dictionary<string, object>>();
        readonly Font detailFont = new Font("Microsoft YaHei UI", 9F);
        readonly Font headlineFont = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold);
        readonly Color ink = Color.FromArgb(38, 55, 78);
        readonly float scale;

        // A tool window remains visible above the current application without taking its keyboard focus.
        protected override bool ShowWithoutActivation { get { return true; } }
        protected override CreateParams CreateParams
        {
            get
            {
                var style = base.CreateParams;
                style.ExStyle |= 0x08000000 | 0x00000080; // WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                return style;
            }
        }

        internal FileChangePopup()
        {
            using (var graphics = CreateGraphics()) scale = graphics.DpiX / 96F;
            FormBorderStyle = FormBorderStyle.None;
            AutoScaleMode = AutoScaleMode.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = false;
            TopMost = true;
            Text = title.Text;
            BackColor = Color.White;
            title.ForeColor = ink;
            title.Font = headlineFont;
            close.Font = headlineFont;
            close.ForeColor = ink;
            close.BackColor = Color.White;
            close.FlatAppearance.BorderSize = 0;
            close.Click += (s, e) => Dismiss();
            Controls.AddRange(new Control[] { title, close, content });
            dismissTimer.Tick += (s, e) => { if (!Bounds.Contains(Cursor.Position)) Dismiss(); };
        }

        int Px(int value) { return (int)Math.Round(value * scale); }

        internal void Present(IList items)
        {
            if (items == null || items.Count == 0) return;
            changes.InsertRange(0, items.OfType<Dictionary<string, object>>().Reverse());
            if (changes.Count == 0) return;
            if (changes.Count > 160) changes.RemoveRange(160, changes.Count - 160);

            Rectangle area = Screen.FromPoint(Cursor.Position).WorkingArea;
            int inset = Px(12), width = Math.Min(Px(388), area.Width - 2 * inset);
            int heading = Px(44), margin = Px(16);
            int lineWidth = Math.Max(1, width - 2 * margin - SystemInformation.VerticalScrollBarWidth);
            content.SuspendLayout();
            content.AutoScrollPosition = Point.Empty;
            while (content.Controls.Count != 0) content.Controls[content.Controls.Count - 1].Dispose();
            int top = 0;
            foreach (var item in changes)
            {
                string path = Json.Text(item, "path").TrimStart('/');
                int split = path.LastIndexOf('/');
                string action = Json.Text(item, "action");
                string verb;
                switch (action)
                {
                    case "create": verb = "已新增"; break;
                    case "edit": verb = "已修改"; break;
                    case "update": verb = "已更新"; break;
                    case "recover": verb = "已恢复"; break;
                    case "copy": verb = "已复制"; break;
                    case "upload": verb = "已上传"; break;
                    default: verb = "操作了"; break;
                }
                string folder = Json.Text(item, "library") + (split < 0 ? "" : " / " + path.Substring(0, split));
                top = AddLine(Json.Text(item, "actor") + " " + verb + " " + path.Substring(split + 1),
                    headlineFont, ink, top, lineWidth);
                top = AddLine(folder, detailFont, Color.FromArgb(81, 99, 123), top + Px(2), lineWidth) + Px(12);
            }
            int height = Math.Min(top, Math.Max(Px(40), Math.Min(Px(270), area.Height - heading - 3 * inset)));
            ClientSize = new Size(width, heading + height + Px(3));
            title.Bounds = new Rectangle(margin, Px(3), width - 2 * margin - Px(32), heading - Px(6));
            close.Bounds = new Rectangle(width - margin - Px(30), Px(6), Px(30), Px(30));
            content.Bounds = new Rectangle(margin, heading, width - 2 * margin, height);
            content.AutoScrollMinSize = new Size(0, top);
            content.ResumeLayout(true);
            Location = new Point(area.Right - Width - inset, area.Bottom - Height - inset);
            if (!Visible) Show();
            dismissTimer.Stop();
            dismissTimer.Start();
        }

        int AddLine(string text, Font font, Color color, int top, int width)
        {
            var line = new Label { Text = text, Font = font, ForeColor = color, UseMnemonic = false,
                AutoSize = true, MaximumSize = new Size(width, 0), Location = new Point(0, top) };
            content.Controls.Add(line);
            return top + line.GetPreferredSize(new Size(width, 0)).Height;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            ControlPaint.DrawBorder(e.Graphics, ClientRectangle, Color.FromArgb(180, 194, 213), ButtonBorderStyle.Solid);
            using (var brush = new SolidBrush(Color.FromArgb(62, 89, 128)))
                e.Graphics.FillRectangle(brush, 0, 0, Width, Px(3));
        }

        internal void Dismiss()
        {
            dismissTimer.Stop();
            Hide();
            changes.Clear();
            while (content.Controls.Count != 0) content.Controls[content.Controls.Count - 1].Dispose();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) dismissTimer.Dispose();
            base.Dispose(disposing);
            if (disposing) { headlineFont.Dispose(); detailFont.Dispose(); }
        }
    }
}
