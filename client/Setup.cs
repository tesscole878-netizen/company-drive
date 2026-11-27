using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Win32;

namespace CompanyDriveSetup
{
    public sealed class SetupForm : Form
    {
        [System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
        static extern int SetCurrentProcessExplicitAppUserModelID(string appID);
        readonly Button install = new Button();
        readonly CheckBox launch = new CheckBox();
        readonly Label status = new Label();
        readonly TextBox location = new TextBox();
        readonly Button browse = new Button();
        const string ConfigKey = @"Software\SuyanCloud\CompanyDrive";
        public SetupForm()
        {
            Text = "安装苏盐新云盘"; ClientSize = new Size(540, 400); FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; MinimizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi; Font = new Font("Microsoft YaHei UI", 9F); BackColor = Color.FromArgb(245, 247, 250);
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application;
            var header = new Panel { Dock = DockStyle.Top, Height = 110, BackColor = Color.FromArgb(52, 68, 91) };
            using (Stream logoStream = typeof(SetupForm).Assembly.GetManifestResourceStream("CompanyDrive.Logo"))
            using (Image logo = Image.FromStream(logoStream))
            {
                header.Controls.Add(new PictureBox { Image = new Bitmap(logo), SizeMode = PictureBoxSizeMode.Zoom,
                    Location = new Point(25, 27), Size = new Size(43, 50), BackColor = Color.White,
                    Padding = new Padding(4) });
            }
            header.Controls.Add(new Label { Text = "把苏盐新云盘，放进电脑。", ForeColor = Color.White, Font = new Font(Font.FontFamily, 19F, FontStyle.Bold), AutoSize = true, Location = new Point(83, 27) });
            header.Controls.Add(new Label { Text = "无需命令行 · 登录后连接电脑盘", ForeColor = Color.FromArgb(178, 191, 211), AutoSize = true, Location = new Point(85, 76) }); Controls.Add(header);
            Controls.Add(new Label { Text = "将为当前 Windows 用户安装连接助手，并创建桌面快捷方式。", AutoSize = true, Location = new Point(28, 139) });
            Controls.Add(new Label { Text = "安装位置", AutoSize = true, Location = new Point(28, 179), ForeColor = Color.Gray });
            location.Text = GetInstallFolder();
            location.BorderStyle = BorderStyle.FixedSingle;
            location.Bounds = new Rectangle(28, 204, 390, 28);
            location.AccessibleName = "安装位置";
            Controls.Add(location);
            browse.Text = "浏览…";
            browse.Bounds = new Rectangle(428, 202, 84, 31);
            browse.Click += (s, e) => ChooseInstallFolder();
            Controls.Add(browse);
            launch.Text = "安装完成后打开连接助手"; launch.Checked = true; launch.AutoSize = true; launch.Location = new Point(28, 252); Controls.Add(launch);
            install.Text = "安装连接助手"; install.Location = new Point(28, 290); install.Size = new Size(484, 43); install.BackColor = Color.FromArgb(73, 99, 140); install.ForeColor = Color.White; install.FlatStyle = FlatStyle.Flat;
            install.Click += async (s, e) => await Install(); Controls.Add(install);
            status.Text = "首次连接需安装 WinFsp 磁盘组件，届时由 Windows 请求管理员确认。";
            status.Location = new Point(28, 349); status.Size = new Size(484, 45); status.ForeColor = Color.Gray; Controls.Add(status);
        }

        static string GetInstallFolder()
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(ConfigKey))
            {
                string previous = key == null ? null : key.GetValue("InstallFolder") as string;
                if (!String.IsNullOrEmpty(previous) && File.Exists(Path.Combine(previous, "CompanyDriveClient.exe")))
                    return previous;
            }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CompanyDrive", "app");
        }

        void ChooseInstallFolder()
        {
            using (var chooser = new FolderBrowserDialog())
            {
                chooser.Description = "选择苏盐新云盘连接助手的安装位置";
                chooser.ShowNewFolderButton = true;
                if (Directory.Exists(location.Text)) chooser.SelectedPath = location.Text;
                if (chooser.ShowDialog(this) == DialogResult.OK)
                {
                    string selected = chooser.SelectedPath;
                    location.Text = File.Exists(Path.Combine(selected, "CompanyDriveClient.exe"))
                        || String.Equals(Path.GetFileName(selected), "苏盐新云盘", StringComparison.OrdinalIgnoreCase)
                        ? selected : Path.Combine(selected, "苏盐新云盘");
                }
            }
        }

        static string ResolveInstallFolder(string input)
        {
            string path = Environment.ExpandEnvironmentVariables(input.Trim().Trim('"'));
            if (path.Length < 3 || path[1] != ':' || (path[2] != '\\' && path[2] != '/'))
                throw new InvalidOperationException("请选择电脑上的完整安装目录。");
            path = Path.GetFullPath(path);
            string volume = Path.GetPathRoot(path);
            if (new DriveInfo(volume).DriveType != DriveType.Fixed)
                throw new InvalidOperationException("请选择本机固定磁盘上的安装目录。");
            if (String.Equals(path.TrimEnd('\\'), volume.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                path = Path.Combine(volume, "苏盐新云盘");
            return path;
        }

        static void SaveInstallFolder(string root, string exe)
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(ConfigKey))
                key.SetValue("InstallFolder", root);
            using (RegistryKey startup = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                if (startup != null && startup.GetValue("CompanyDriveClient") != null)
                    startup.SetValue("CompanyDriveClient", "\"" + exe + "\" --background");
        }

        async Task Install()
        {
            install.Enabled = false; launch.Enabled = false; location.Enabled = false; browse.Enabled = false;
            status.Text = "正在安装文件，请稍候…";
            try
            {
                string installRoot = ResolveInstallFolder(location.Text);
                location.Text = installRoot;
                string previous = GetInstallFolder();
                int session = Process.GetCurrentProcess().SessionId;
                foreach (Process running in Process.GetProcessesByName("CompanyDriveClient"))
                    using (running)
                        if (running.SessionId == session)
                            throw new InvalidOperationException("请先在右下角托盘退出连接助手，再继续安装。");
                bool openAfter = launch.Checked;
                await Task.Run(() =>
                {
                    Directory.CreateDirectory(installRoot);
                    string settings = Path.Combine(installRoot, "company.json");
                    string previousSettings = Path.Combine(previous, "company.json");
                    if (!File.Exists(settings) && File.Exists(previousSettings))
                        File.Copy(previousSettings, settings);
                    ExtractPayload(installRoot);
                });
                string exe = Path.Combine(installRoot, "CompanyDriveClient.exe");
                CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "苏盐新云盘.lnk"), exe);
                string menu = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "苏盐新云盘"); Directory.CreateDirectory(menu);
                CreateShortcut(Path.Combine(menu, "苏盐新云盘.lnk"), exe);
                SaveInstallFolder(installRoot, exe);
                CompanyDrive.DriveMountCleanup.RemoveDisconnected();
                status.Text = "安装完成。可以从桌面的“苏盐新云盘”快捷方式打开。";
                install.Text = "安装完成";
                if (openAfter) Process.Start(new ProcessStartInfo(exe) { UseShellExecute = true });
            }
            catch (Exception e)
            {
                status.Text = "安装未完成：" + e.Message;
                install.Enabled = true; launch.Enabled = true; location.Enabled = true; browse.Enabled = true;
            }
        }
        public static void ExtractPayload(string root)
        {
            string fullRoot = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Directory.CreateDirectory(fullRoot);
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
            using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
            {
                foreach (var entry in archive.Entries)
                {
                    string target = Path.GetFullPath(Path.Combine(fullRoot, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                    if (!target.StartsWith(fullRoot, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("安装包路径无效。");
                    if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(target); continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    // An administrator-supplied address survives an upgrade.
                    if (entry.Name == "company.json" && File.Exists(target))
                    {
                        try
                        {
                            var settings = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(target));
                            if (settings != null && settings.ContainsKey("server_url")
                                && !String.IsNullOrWhiteSpace(Convert.ToString(settings["server_url"]))) continue;
                        }
                        catch (ArgumentException) { }
                    }
                    using (Stream input = entry.Open())
                    using (var output = new FileStream(target, FileMode.Create, FileAccess.Write)) input.CopyTo(output);
                }
            }
        }
        static void CreateShortcut(string path, string target)
        {
            Type type = Type.GetTypeFromProgID("WScript.Shell"); object shell = Activator.CreateInstance(type);
            object shortcut = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
            Type shortcutType = shortcut.GetType();
            shortcutType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { target });
            shortcutType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { Path.GetDirectoryName(target) });
            shortcutType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { target + ",0" });
            shortcutType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "登录苏盐新云盘并连接为电脑盘" });
            shortcutType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, new object[0]);
            System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shortcut); System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shell);
        }
        [STAThread]
        static void Main(string[] args)
        {
            SetCurrentProcessExplicitAppUserModelID("CompanyDrive.Setup");
            if (args.Length == 2 && args[0] == "--verify-extract") { ExtractPayload(args[1]); return; }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false); Application.Run(new SetupForm());
        }
    }
}
