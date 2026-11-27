using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace CompanyDrive
{
    internal sealed class EncryptedLibrariesForm : Form
    {
        const string Route = "/dav/.suyan-encrypted";
        readonly Profile profile;
        readonly HttpClient client = new HttpClient(CompanyCertificate.CreateHandler())
            { Timeout = TimeSpan.FromSeconds(25) };
        readonly ListView entries = new ListView { View = View.Details, FullRowSelect = true, MultiSelect = false, Dock = DockStyle.Fill };
        readonly Button back = new Button { Text = "上一级", AutoSize = true };
        readonly Button open = new Button { Text = "打开", AutoSize = true };
        readonly Button download = new Button { Text = "下载副本", AutoSize = true };
        readonly Button cancel = new Button { Text = "取消操作", AutoSize = true };
        readonly Label status = new Label { AutoSize = true, Text = "正在读取加密资料库…" };
        CancellationTokenSource operation;
        string libraryId, libraryName;
        string folder = "/";
        bool closing;

        sealed class Entry
        {
            public string Name, Id;
            public bool Folder, Unlocked;
        }

        public EncryptedLibrariesForm(Profile profile)
        {
            this.profile = profile;
            Text = "旧 Seafile 加密资料库";
            Font = new Font("Microsoft YaHei UI", 9);
            AutoScaleMode = AutoScaleMode.Font;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(640, 420);
            MinimumSize = new Size(530, 320);
            entries.Columns.Add("名称", 450);
            entries.Columns.Add("类型", 115);
            var toolbar = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            toolbar.Controls.AddRange(new Control[] { back, open, download, cancel });
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(12), RowCount = 3, ColumnCount = 1 };
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(toolbar, 0, 0);
            layout.Controls.Add(entries, 0, 1);
            layout.Controls.Add(status, 0, 2);
            Controls.Add(layout);
            Shown += async (s, e) => await Run(ShowLibraries);
            entries.SelectedIndexChanged += (s, e) => RefreshActions();
            entries.DoubleClick += async (s, e) => await Run(OpenEntry);
            entries.KeyDown += async (s, e) => {
                if (e.KeyCode == Keys.Enter) { e.Handled = e.SuppressKeyPress = true; await Run(OpenEntry); }
            };
            open.Click += async (s, e) => await Run(OpenEntry);
            download.Click += async (s, e) => await Run(ct => SaveFile(Selected, false, ct));
            back.Click += async (s, e) => await Run(GoUp);
            cancel.Click += (s, e) => { if (operation != null) operation.Cancel(); };
            RefreshActions();
        }

        Entry Selected { get { return entries.SelectedItems.Count > 0 ? (Entry)entries.SelectedItems[0].Tag : null; } }
        string LibraryRoute { get { return Route + "/" + Uri.EscapeDataString(libraryId); } }

        void RefreshActions()
        {
            bool ready = !closing && operation == null;
            var entry = Selected;
            open.Enabled = ready && entry != null;
            download.Enabled = ready && entry != null && !entry.Folder;
            back.Enabled = ready && libraryId != null;
            cancel.Enabled = operation != null;
        }

        async Task Run(Func<CancellationToken, Task> action)
        {
            if (closing || operation != null) return;
            operation = new CancellationTokenSource();
            RefreshActions();
            bool locked = false;
            try { await action(operation.Token); }
            catch (OperationCanceledException) { status.Text = "操作已取消或连接超时"; }
            catch (LibraryLocked) { locked = true; }
            catch (Exception error) { status.Text = error.Message; }
            if (locked)
            {
                entries.Items.Clear();
                libraryId = null;
                try { await ShowLibraries(operation.Token); }
                catch (Exception) { }
                status.Text = "加密库已锁定，请再次打开并输入原库密码。";
            }
            operation.Dispose();
            operation = null;
            RefreshActions();
        }

        async Task<HttpResponseMessage> Send(HttpMethod method, string path, CancellationToken ct, object data = null)
        {
            using (var request = new HttpRequestMessage(method, profile.Server + path))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Basic",
                    Convert.ToBase64String(Encoding.UTF8.GetBytes(profile.Username + ":" + profile.DevicePassword)));
                request.Headers.Add("X-Suyan-Encrypted", "1");
                if (data != null)
                    request.Content = new StringContent(Json.Write(data), Encoding.UTF8, "application/json");
                var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
                if (response.IsSuccessStatusCode) return response;
                using (response)
                {
                    if ((int)response.StatusCode == 423) throw new LibraryLocked();
                    string detail = "加密资料库暂时不可用，请检查云盘连接和当前权限。";
                    try { detail = Json.Text(Json.Read(await response.Content.ReadAsStringAsync()), "detail", detail); }
                    catch (ArgumentException) { }
                    catch (InvalidOperationException) { }
                    throw new InvalidOperationException(detail);
                }
            }
        }

        async Task<List<Dictionary<string, object>>> ReadRows(string path, CancellationToken ct)
        {
            using (var response = await Send(HttpMethod.Get, path, ct))
            {
                var data = Json.Serializer.Deserialize<ArrayList>(await response.Content.ReadAsStringAsync());
                return data.Cast<Dictionary<string, object>>().ToList();
            }
        }

        void Add(Entry entry, string kind)
        {
            var row = new ListViewItem(new[] { entry.Name.Replace("\u007f", "␡"), kind });
            row.Tag = entry;
            entries.Items.Add(row);
        }

        async Task ShowLibraries(CancellationToken ct)
        {
            var rows = await ReadRows(Route + "/libraries", ct);
            libraryId = null;
            libraryName = null;
            entries.Items.Clear();
            foreach (var row in rows)
            {
                bool unlocked = Object.Equals(row["unlocked"], true);
                Add(new Entry { Name = Json.Text(row, "name"), Id = Json.Text(row, "id"), Folder = true, Unlocked = unlocked },
                    unlocked ? "已解锁" : "加密库");
            }
            status.Text = rows.Count == 0 ? "这个账号没有可查看的旧 Seafile 加密库。" : "打开资料库并输入原库密码。";
        }

        async Task ShowFolder(string path, CancellationToken ct)
        {
            var rows = await ReadRows(LibraryRoute + "/files?path=" + Uri.EscapeDataString(path), ct);
            folder = path;
            entries.BeginUpdate();
            try
            {
                entries.Items.Clear();
                foreach (var row in rows)
                {
                    bool directory = Object.Equals(row["is_dir"], true);
                    Add(new Entry { Name = Json.Text(row, "name"), Folder = directory }, directory ? "文件夹" : "文件");
                }
            }
            finally { entries.EndUpdate(); }
            status.Text = (libraryName + path).Replace("\u007f", "␡") + " · 只读";
        }

        string EntryPath(Entry entry) { return folder.TrimEnd('/') + "/" + entry.Name; }

        async Task OpenEntry(CancellationToken ct)
        {
            var entry = Selected;
            if (entry == null) return;
            if (libraryId == null)
            {
                if (!entry.Unlocked)
                {
                    using (var dialog = new Form { Text = "解锁 " + entry.Name, Font = Font, StartPosition = FormStartPosition.CenterParent,
                        FormBorderStyle = FormBorderStyle.FixedDialog, MinimizeBox = false, MaximizeBox = false, ClientSize = new Size(358, 112) })
                    {
                        var prompt = new Label { Text = "原 Seafile 资料库密码", AutoSize = true, Location = new Point(12, 10) };
                        var input = new TextBox { UseSystemPasswordChar = true, MaxLength = 400, Location = new Point(12, 34), Width = 333 };
                        var confirm = new Button { Text = "解锁", DialogResult = DialogResult.OK, Location = new Point(270, 70) };
                        dialog.Controls.AddRange(new Control[] { prompt, input, confirm });
                        dialog.AcceptButton = confirm;
                        dialog.ActiveControl = input;
                        if (dialog.ShowDialog(this) != DialogResult.OK || input.Text.Length == 0) return;
                        try
                        {
                            using (await Send(HttpMethod.Post, Route + "/" + Uri.EscapeDataString(entry.Id) + "/unlock", ct,
                                new { password = input.Text })) { }
                        }
                        finally { input.Clear(); }
                    }
                }
                libraryId = entry.Id;
                libraryName = entry.Name;
                try { await ShowFolder("/", ct); }
                catch { libraryId = null; libraryName = null; throw; }
                return;
            }
            if (entry.Folder) await ShowFolder(EntryPath(entry), ct);
            else await SaveFile(entry, true, ct);
        }

        Task GoUp(CancellationToken ct)
        {
            return folder == "/" ? ShowLibraries(ct) : ShowFolder(folder.Substring(0, Math.Max(1, folder.LastIndexOf('/'))), ct);
        }

        async Task SaveFile(Entry entry, bool openFile, CancellationToken ct)
        {
            if (entry == null || entry.Folder) return;
            char[] invalid = Path.GetInvalidFileNameChars();
            string suggestion = new string(entry.Name.Select(ch => invalid.Contains(ch) || ch == 127 ? '_' : ch).ToArray()).TrimEnd(' ', '.');
            using (var dialog = new SaveFileDialog { Title = openFile ? "保存副本并打开" : "下载加密库文件副本",
                FileName = suggestion, Filter = "所有文件|*.*", AddExtension = false, RestoreDirectory = true })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                string destination = dialog.FileName;
                string staging = Path.Combine(Path.GetDirectoryName(destination), ".suyan-" + Guid.NewGuid().ToString("N") + ".part");
                status.Text = "正在下载文件副本…";
                try
                {
                    using (var response = await Send(HttpMethod.Get, LibraryRoute + "/download?path=" + Uri.EscapeDataString(EntryPath(entry)), ct))
                    using (var source = await response.Content.ReadAsStreamAsync())
                    using (var file = new FileStream(staging, FileMode.CreateNew, FileAccess.Write, FileShare.None, 128 * 1024, true))
                    {
                        await source.CopyToAsync(file, 128 * 1024, ct);
                        long? expected = response.Content.Headers.ContentLength;
                        if (expected.HasValue && file.Length != expected.Value)
                            throw new IOException("文件下载不完整，请重试。");
                    }
                    ct.ThrowIfCancellationRequested();
                    if (File.Exists(destination)) File.Replace(staging, destination, null);
                    else File.Move(staging, destination);
                }
                finally { if (File.Exists(staging)) File.Delete(staging); }
                status.Text = "副本已保存到 " + destination;
                if (openFile) Process.Start(new ProcessStartInfo(destination) { UseShellExecute = true });
            }
        }

        sealed class LibraryLocked : Exception { }

        protected override async void OnFormClosing(FormClosingEventArgs e)
        {
            base.OnFormClosing(e);
            if (operation != null) { e.Cancel = true; operation.Cancel(); return; }
            if (closing || e.Cancel || e.CloseReason == CloseReason.WindowsShutDown) return;
            e.Cancel = true;
            closing = true;
            RefreshActions();
            using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                try { using (await Send(HttpMethod.Post, Route + "/lock", timeout.Token)) { } }
                catch (Exception) { }
            Close();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) client.Dispose();
            base.Dispose(disposing);
        }
    }
}
