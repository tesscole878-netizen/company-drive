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
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CompanyDrive
{
    public static class Json
    {
        public static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();
        public static string Write(object value) { return Serializer.Serialize(value); }
        public static Dictionary<string, object> Read(string value) { return Serializer.Deserialize<Dictionary<string, object>>(value); }
        public static string Text(Dictionary<string, object> value, string key, string fallback = "")
        { return value != null && value.ContainsKey(key) && value[key] != null ? Convert.ToString(value[key]) : fallback; }
    }

    public sealed class Profile
    {
        public string Server = "";
        public string Username = "";
        public string DevicePassword = "";
        public string TokenId = "";
        public string Letter = "Z";
        public string Cache = "";
        public bool Remember = true;
        public bool RememberPassword;
        public bool AutoMount;
        public bool AutoStart = false;
    }

    public static class LocalSettings
    {
        public static string Folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CompanyDriveClient");
        public static readonly byte[] Entropy = Encoding.UTF8.GetBytes("CompanyDriveClient/profile/v1");
        public static string NormalizeServer(string input)
        {
            Uri uri;
            input = (input ?? "").Trim().TrimEnd('/');
            if (input.EndsWith("/dav", StringComparison.OrdinalIgnoreCase)) input = input.Substring(0, input.Length - 4);
            if (!Uri.TryCreate(input, UriKind.Absolute, out uri) || (uri.Scheme != "http" && uri.Scheme != "https")
                || !String.IsNullOrEmpty(uri.UserInfo) || !String.IsNullOrEmpty(uri.Query) || !String.IsNullOrEmpty(uri.Fragment)
                || uri.AbsolutePath != "/")
                throw new InvalidOperationException("请填写云盘网站首页地址，例如 https://drive.example.com，不要填写账号、密码或文件路径。");
            if (uri.Scheme == "http" && !uri.IsLoopback)
            {
                IPAddress address;
                bool privateIp = IPAddress.TryParse(uri.Host, out address) && IsPrivate(address);
                if (!privateIp) throw new InvalidOperationException("公司外访问请使用 HTTPS 地址。内网试点可以填写 NAS 的局域网 IP。");
            }
            return uri.GetLeftPart(UriPartial.Authority).TrimEnd('/');
        }
        private static bool IsPrivate(IPAddress address)
        {
            byte[] b = address.GetAddressBytes();
            if (b.Length == 4) return b[0] == 10 || b[0] == 127 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31) || (b[0] == 192 && b[1] == 168);
            return IPAddress.IsLoopback(address) || address.IsIPv6LinkLocal || (b[0] & 0xfe) == 0xfc;
        }
        public static string CacheKey(string server, string username)
        {
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(server + "|" + username.ToLowerInvariant()))).Replace("-", "").Substring(0, 20);
        }
        public static Profile Load()
        {
            string path = Path.Combine(Folder, "profile.bin");
            if (!File.Exists(path)) return new Profile();
            Profile profile;
            bool migrate;
            try
            {
                var saved = Json.Read(Encoding.UTF8.GetString(ProtectedData.Unprotect(File.ReadAllBytes(path), Entropy, DataProtectionScope.CurrentUser)));
                migrate = saved.Remove("DevicePassword");
                object autoConnect;
                if (saved.TryGetValue("AutoConnect", out autoConnect))
                {
                    saved["AutoStart"] = Object.Equals(autoConnect, true);
                    saved.Remove("AutoConnect");
                    migrate = true;
                }
                profile = Json.Serializer.ConvertToType<Profile>(saved);
                if (!profile.Remember) profile.Username = "";
                if (profile.Remember && profile.RememberPassword && !String.IsNullOrEmpty(profile.Username))
                    profile.DevicePassword = Json.Text(saved, "SavedDeviceCredential");
                profile.RememberPassword = !String.IsNullOrEmpty(profile.DevicePassword);
                profile.AutoMount &= profile.RememberPassword && profile.AutoStart;
            }
            catch { return new Profile(); }
            if (migrate)
            {
                try { Save(profile); }
                catch (IOException error) { Trace.TraceWarning(error.Message); }
                catch (UnauthorizedAccessException error) { Trace.TraceWarning(error.Message); }
            }
            return profile;
        }
        public static void Save(Profile profile)
        {
            Directory.CreateDirectory(Folder);
            bool rememberPassword = profile.Remember && profile.RememberPassword && !String.IsNullOrEmpty(profile.DevicePassword);
            var settings = new { profile.Server, Username = profile.Remember ? profile.Username : "",
                TokenId = profile.Remember ? profile.TokenId : "", profile.Letter, profile.Cache, profile.Remember, profile.AutoStart,
                RememberPassword = rememberPassword, SavedDeviceCredential = rememberPassword ? profile.DevicePassword : "",
                AutoMount = rememberPassword && profile.AutoStart && profile.AutoMount };
            byte[] data = ProtectedData.Protect(Encoding.UTF8.GetBytes(Json.Write(settings)), Entropy, DataProtectionScope.CurrentUser);
            string target = Path.Combine(Folder, "profile.bin");
            string temp = target + ".new";
            File.WriteAllBytes(temp, data);
            if (File.Exists(target)) File.Replace(temp, target, null); else File.Move(temp, target);
        }
        public static string DefaultServer()
        {
            string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "company.json");
            try { return Json.Text(Json.Read(File.ReadAllText(path, Encoding.UTF8)), "server_url"); } catch { return ""; }
        }
        public static string WinFspFolder()
        {
            foreach (RegistryView view in new[] { RegistryView.Registry32, RegistryView.Registry64 })
                using (RegistryKey root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, view))
                using (RegistryKey key = root.OpenSubKey(@"SOFTWARE\WinFsp"))
                {
                    string path = key == null ? null : key.GetValue("InstallDir") as string;
                    if (!String.IsNullOrEmpty(path) && File.Exists(Path.Combine(path, "bin", "winfsp-x64.dll"))) return path;
                }
            string standard = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "WinFsp");
            return File.Exists(Path.Combine(standard, "bin", "winfsp-x64.dll")) ? standard : null;
        }
        public static void SetAutoStart(bool enabled)
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
            {
                if (enabled) key.SetValue("CompanyDriveClient", RcloneEngine.Quote(Application.ExecutablePath) + " --background");
                else key.DeleteValue("CompanyDriveClient", false);
            }
        }
    }

    public static class CompanyCertificate
    {
        static readonly byte[] rootData = ReadRoot();

        static byte[] ReadRoot()
        {
            using (Stream stream = typeof(CompanyCertificate).Assembly.GetManifestResourceStream("CompanyDrive.RootCertificate"))
            using (var data = new MemoryStream())
            {
                if (stream == null) throw new InvalidOperationException("连接助手缺少公司证书，请安装最新版。");
                stream.CopyTo(data);
                return data.ToArray();
            }
        }

        public static WebRequestHandler CreateHandler()
        {
            return new WebRequestHandler {
                CookieContainer = new CookieContainer(), AllowAutoRedirect = false, UseProxy = false,
                ServerCertificateValidationCallback = Validate
            };
        }

        public static bool Validate(object sender, X509Certificate certificate, X509Chain systemChain, SslPolicyErrors errors)
        {
            if (errors == SslPolicyErrors.None) return true;
            if (errors != SslPolicyErrors.RemoteCertificateChainErrors || certificate == null) return false;
            try
            {
                using (var root = new X509Certificate2(rootData))
                using (var leaf = new X509Certificate2(certificate))
                using (var chain = new X509Chain())
                {
                    chain.ChainPolicy.ExtraStore.Add(root);
                    chain.ChainPolicy.VerificationFlags = X509VerificationFlags.AllowUnknownCertificateAuthority;
                    chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
                    chain.ChainPolicy.ApplicationPolicy.Add(new Oid("1.3.6.1.5.5.7.3.1"));
                    if (!chain.Build(leaf) || chain.ChainElements.Count < 2) return false;
                    var issuer = chain.ChainElements[chain.ChainElements.Count - 1].Certificate;
                    if (!issuer.RawData.SequenceEqual(rootData)) return false;
                    return chain.ChainStatus.All(status => status.Status == X509ChainStatusFlags.NoError
                        || status.Status == X509ChainStatusFlags.UntrustedRoot);
                }
            }
            catch (CryptographicException) { return false; }
        }

        public static string BundlePath()
        {
            Directory.CreateDirectory(LocalSettings.Folder);
            string path = Path.Combine(LocalSettings.Folder, "company-ca.pem");
            string pem = "-----BEGIN CERTIFICATE-----\n"
                + Convert.ToBase64String(rootData, Base64FormattingOptions.InsertLineBreaks)
                + "\n-----END CERTIFICATE-----\n";
            if (!File.Exists(path) || File.ReadAllText(path) != pem)
                File.WriteAllText(path, pem, Encoding.ASCII);
            return path;
        }
    }

    public sealed class CloudApi : IDisposable
    {
        readonly HttpClient client;
        readonly string server;
        string csrf = "";
        public CloudApi(string address)
        {
            server = LocalSettings.NormalizeServer(address);
            var handler = CompanyCertificate.CreateHandler();
            client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(25) };
        }
        public async Task<Dictionary<string, object>> Call(string method, string path, object body = null)
        {
            using (var request = new HttpRequestMessage(new HttpMethod(method), server + path))
            {
                if (body != null) request.Content = new StringContent(Json.Write(body), Encoding.UTF8, "application/json");
                if (!String.IsNullOrEmpty(csrf)) request.Headers.Add("X-Drive-CSRF", csrf);
                HttpResponseMessage response;
                try { response = await client.SendAsync(request); }
                catch (TaskCanceledException) { throw new InvalidOperationException("连接超时，请检查公司网络、VPN 和云盘地址。"); }
                catch (HttpRequestException) { throw new InvalidOperationException("无法连接云盘，请检查地址、网络和 HTTPS 证书。"); }
                using (response)
                {
                    string text = await response.Content.ReadAsStringAsync();
                    Dictionary<string, object> data;
                    try { data = Json.Read(text); } catch { throw new InvalidOperationException("这个地址没有返回云盘接口，请确认填写的是苏盐新云盘首页地址。"); }
                    if (!response.IsSuccessStatusCode) throw new InvalidOperationException(Json.Text(data, "detail", "请求失败，请稍后重试。"));
                    return data;
                }
            }
        }
        public async Task<string> Login(string username, string password)
        {
            var result = await Call("POST", "/api/login", new { username = username, password = password });
            csrf = Json.Text(result, "csrf");
            if (String.IsNullOrEmpty(csrf)) throw new InvalidOperationException("云盘版本不兼容，请联系管理员。");
            return Json.Text(result, "name", username);
        }
        public async Task<Dictionary<string, object>> NewDevice()
        { return await Call("POST", "/api/tokens", new { name = Environment.MachineName + " · Windows 连接助手" }); }
        public async Task Revoke(string id) { await Call("DELETE", "/api/tokens/" + Uri.EscapeDataString(id)); }
        public static async Task<Dictionary<string, object>> FileChanges(HttpClient client, Profile profile, string cursor)
        {
            string url = profile.Server + "/dav/?file-changes=1&refresh=1" + (cursor.Length == 0 ? "" : "&cursor=" + Uri.EscapeDataString(cursor));
            using (var request = new HttpRequestMessage(HttpMethod.Get, url))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Basic",
                    Convert.ToBase64String(Encoding.UTF8.GetBytes(profile.Username + ":" + profile.DevicePassword)));
                using (var response = await client.SendAsync(request))
                {
                    response.EnsureSuccessStatusCode();
                    return Json.Read(await response.Content.ReadAsStringAsync());
                }
            }
        }

        public static async Task CheckDevice(Profile profile)
        {
            using (var client = new HttpClient(CompanyCertificate.CreateHandler()) { Timeout = TimeSpan.FromSeconds(20) })
            using (var request = new HttpRequestMessage(new HttpMethod("PROPFIND"), profile.Server + "/dav/"))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Basic", Convert.ToBase64String(Encoding.UTF8.GetBytes(profile.Username + ":" + profile.DevicePassword)));
                request.Headers.Add("Depth", "0");
                using (var response = await client.SendAsync(request))
                {
                    if (response.StatusCode == HttpStatusCode.Unauthorized || response.StatusCode == HttpStatusCode.Forbidden)
                        throw new DeviceAccessDenied();
                    if ((int)response.StatusCode != 207) throw new InvalidOperationException("暂时无法验证电脑连接，请稍后重试。");
                }
            }
        }
        public void Dispose() { client.Dispose(); }
    }

    internal sealed class DeviceAccessDenied : InvalidOperationException
    {
        public DeviceAccessDenied() : base("设备授权已失效，请重新输入网页登录密码。") { }
    }

    public sealed class RcloneEngine
    {
        public Process Process;
        public string Letter;
        public string LastError = "";
        HttpClient rc;
        public bool Running { get { try { return Process != null && !Process.HasExited; } catch { return false; } } }
        public static string Quote(string value)
        {
            // Windows CommandLineToArgvW-compatible escaping, including trailing backslashes.
            var b = new StringBuilder("\""); int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append('"'); slashes = 0; continue; }
                b.Append('\\', slashes); slashes = 0; b.Append(c);
            }
            b.Append('\\', slashes * 2); b.Append('"'); return b.ToString();
        }
        static ProcessStartInfo StartInfo(string exe, string args)
        {
            return new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        }
        public static async Task<string> Obscure(string exe, string password)
        {
            var info = StartInfo(exe, "obscure -"); info.RedirectStandardInput = true;
            using (var process = Process.Start(info))
            {
                await process.StandardInput.WriteLineAsync(password); process.StandardInput.Close();
                string text = await process.StandardOutput.ReadToEndAsync();
                string error = await process.StandardError.ReadToEndAsync();
                await Task.Run(() => process.WaitForExit());
                if (process.ExitCode != 0) throw new InvalidOperationException("无法初始化连接组件。");
                return text.Trim();
            }
        }
        public static async Task<ProcessStartInfo> Configure(Profile profile, string exe, string args)
        {
            Directory.CreateDirectory(LocalSettings.Folder);
            string empty = Path.Combine(LocalSettings.Folder, "empty.conf");
            if (!File.Exists(empty)) File.WriteAllText(empty, "", Encoding.ASCII);
            var info = StartInfo(exe, "--config " + Quote(empty) + " " + args);
            // This public CA belongs to the packaged company deployment.
            if (LocalSettings.NormalizeServer(profile.Server).Equals("https://192.168.80.252:8443", StringComparison.OrdinalIgnoreCase))
                info.Arguments += " --ca-cert " + Quote(CompanyCertificate.BundlePath());
            info.EnvironmentVariables["RCLONE_CONFIG_COMPANY_TYPE"] = "webdav";
            info.EnvironmentVariables["RCLONE_CONFIG_COMPANY_URL"] = profile.Server + "/dav/";
            // Include server modification time when deciding whether a cached file is current.
            info.EnvironmentVariables["RCLONE_CONFIG_COMPANY_VENDOR"] = "rclone";
            info.EnvironmentVariables["RCLONE_CONFIG_COMPANY_USER"] = profile.Username;
            info.EnvironmentVariables["RCLONE_CONFIG_COMPANY_PASS"] = await Obscure(exe, profile.DevicePassword);
            // The API and mounted drive use the same direct connection to this server.
            info.EnvironmentVariables["NO_PROXY"] = "localhost,127.0.0.1,::1," + new Uri(profile.Server).Host;
            return info;
        }
        public async Task Start(Profile profile)
        {
            if (Running) throw new InvalidOperationException("云盘已经连接。");
            if (!CleanupExited()) DriveMountCleanup.RemoveDisconnected();
            if (LocalSettings.WinFspFolder() == null) throw new InvalidOperationException("请先点击“安装磁盘组件”。安装只需进行一次。");
            if (profile.Letter.Length != 1 || profile.Letter[0] < 'D' || profile.Letter[0] > 'Z') throw new InvalidOperationException("请选择 D 到 Z 之间的空闲盘符。");
            if (DriveInfo.GetDrives().Any(d => d.Name.StartsWith(profile.Letter + ":", StringComparison.OrdinalIgnoreCase)))
                throw new InvalidOperationException(profile.Letter + ": 盘已被使用，请选择其他盘符。");
            string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "rclone.exe");
            if (!File.Exists(exe)) throw new InvalidOperationException("缺少连接组件，请重新安装苏盐新云盘助手。");
            string cache = Path.Combine(Path.GetFullPath(profile.Cache), LocalSettings.CacheKey(profile.Server, profile.Username));
            Directory.CreateDirectory(cache);
            string test = Path.Combine(cache, Guid.NewGuid().ToString("N") + ".test");
            File.WriteAllText(test, ""); File.Delete(test);
            var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
            int port = ((IPEndPoint)listener.LocalEndpoint).Port; listener.Stop();
            string rcPassword = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
            string args = "mount company: " + profile.Letter + ": --network-mode --volname " + Quote(DriveMountCleanup.MountName(profile.Letter))
                + " --vfs-cache-mode full --dir-cache-time 30s --vfs-cache-max-size 5G --vfs-cache-min-free-space 2G --vfs-write-back 3s"
                + " --cache-dir " + Quote(cache) + " --log-level ERROR --rc --rc-addr 127.0.0.1:" + port;
            var info = await Configure(profile, exe, args);
            info.EnvironmentVariables["RCLONE_RC_USER"] = "drive";
            info.EnvironmentVariables["RCLONE_RC_PASS"] = rcPassword;
            info.EnvironmentVariables["RCLONE_RC_NO_AUTH"] = "false";
            rc = new HttpClient(new HttpClientHandler { UseProxy = false }) { BaseAddress = new Uri("http://127.0.0.1:" + port), Timeout = TimeSpan.FromSeconds(5) };
            rc.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Basic", Convert.ToBase64String(Encoding.UTF8.GetBytes("drive:" + rcPassword)));
            Letter = profile.Letter; LastError = "";
            Process = new Process { StartInfo = info, EnableRaisingEvents = true };
            Process.ErrorDataReceived += (s, e) => { if (!String.IsNullOrEmpty(e.Data)) LastError = e.Data; };
            Process.OutputDataReceived += (s, e) => { };
            bool mounted = false;
            try
            {
                Process.Start(); Process.BeginErrorReadLine(); Process.BeginOutputReadLine();
                for (int i = 0; i < 60; i++)
                {
                    if (!Running) throw new InvalidOperationException("挂载失败，请检查网络、磁盘组件和盘符。" + (LastError.Contains("WinFsp") ? "请重新安装磁盘组件后再试。" : ""));
                    if (Directory.Exists(Letter + @":\"))
                    {
                        mounted = true;
                        DriveMountCleanup.RefreshAfterConnect(profile.Letter);
                        FollowUpCleanup(profile.Letter);
                        return;
                    }
                    await Task.Delay(500);
                }
                try { await Stop(); } catch { }
                throw new InvalidOperationException("连接耗时较长，请检查网络后重试；已保留本地缓存。");
            }
            finally
            {
                if (!mounted && CleanupExited()) FollowUpCleanup(profile.Letter);
            }
        }

        static async void FollowUpCleanup(string letter)
        {
            await Task.Delay(1200);
            DriveMountCleanup.RemoveDisconnected(letter);
        }
        public async Task<Dictionary<string, object>> Rc(string path, object args = null)
        {
            if (!Running || rc == null) throw new InvalidOperationException("云盘没有连接。");
            using (var response = await rc.PostAsync("/" + path, new StringContent(args == null ? "{}" : Json.Write(args), Encoding.UTF8, "application/json")))
            {
                string text = await response.Content.ReadAsStringAsync();
                if (!response.IsSuccessStatusCode) throw new InvalidOperationException("无法读取上传状态，请稍后重试。");
                return Json.Read(text);
            }
        }

        [System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, ExactSpelling = true)]
        static extern void SHChangeNotify(int eventId, uint flags, string item1, IntPtr item2);

        public async Task ForgetDirectories(IEnumerable<string> paths)
        {
            var changes = new Dictionary<string, string>();
            char[] invalid = Path.GetInvalidFileNameChars();
            foreach (var path in paths.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (String.IsNullOrWhiteSpace(path) || path.Split('/').Any(part => part.Length == 0 || part == "." || part == ".."
                    || part.IndexOfAny(invalid) >= 0)) continue;
                changes.Add("dir" + changes.Count, path);
            }
            if (changes.Count == 0) return;
            // This command expires directory metadata. The disk write-back queue belongs to the VFS cache.
            await Rc("vfs/forget", changes);
            foreach (string path in changes.Values)
            {
                string directory = Letter + @":\" + path.Replace('/', '\\');
                if (directory.Length < 260) SHChangeNotify(0x1000, 0x0005, directory, IntPtr.Zero);
            }
        }
        public async Task<int> Pending()
        {
            var data = await Rc("vfs/stats");
            var cache = data.ContainsKey("diskCache") ? data["diskCache"] as Dictionary<string, object> : null;
            if (cache == null) throw new InvalidOperationException("无法确认文件上传状态，暂不能断开。");
            int total = 0;
            foreach (string key in new[] { "uploadsQueued", "uploadsInProgress", "erroredFiles" })
                if (cache.ContainsKey(key)) total += Convert.ToInt32(cache[key]);
            return total;
        }
        public async Task Stop()
        {
            if (!Running) { await FinishMountCleanup(); return; }
            if (await Pending() > 0) throw new InvalidOperationException("还有文件等待上传或上传失败。请保持网络连接，上传完成后再断开。");
            try { await Rc("core/quit"); } catch (HttpRequestException) { } catch (TaskCanceledException) { }
            for (int i = 0; i < 30 && Running; i++) await Task.Delay(100);
            if (Running) throw new InvalidOperationException("云盘还在结束连接，请稍后重试。");
            await FinishMountCleanup();
        }

        public async Task<bool> FinishMountCleanup()
        {
            string letter = Letter;
            if (!CleanupExited()) return false;
            // WinFsp and Explorer can finish removing the mapping after rclone exits.
            await Task.Delay(1200);
            DriveMountCleanup.RemoveDisconnected(letter);
            return true;
        }

        public bool CleanupExited()
        {
            if (Process == null || Running) return false;
            Process.Dispose();
            Process = null;
            if (rc != null) rc.Dispose();
            rc = null;
            DriveMountCleanup.RemoveDisconnected(Letter);
            return true;
        }
    }

    public static class DriverInstaller
    {
        public const string DownloadUrl = "https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi";
        public static async Task Install(Action<string> progress)
        {
            string directory = Path.Combine(LocalSettings.Folder, "installers"); Directory.CreateDirectory(directory);
            string target = Path.Combine(directory, "winfsp-2.1.25156.msi");
            progress("正在从 WinFsp 官方下载磁盘组件…");
            using (var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = true }) { Timeout = TimeSpan.FromMinutes(5) })
            using (var response = await client.GetAsync(DownloadUrl, HttpCompletionOption.ResponseHeadersRead))
            {
                response.EnsureSuccessStatusCode();
                using (var file = new FileStream(target + ".download", FileMode.Create, FileAccess.Write)) await response.Content.CopyToAsync(file);
            }
            if (File.Exists(target)) File.Delete(target); File.Move(target + ".download", target);
            progress("正在验证官方安装文件签名…");
            var verify = new ProcessStartInfo("powershell.exe", "-NoProfile -NonInteractive -Command \"$s=Get-AuthenticodeSignature -LiteralPath $env:COMPANY_DRIVE_DRIVER_MSI; if($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'NAVIMATICS LLC'){exit 0}else{exit 2}\"")
            { UseShellExecute = false, CreateNoWindow = true };
            verify.EnvironmentVariables["COMPANY_DRIVE_DRIVER_MSI"] = target;
            using (var p = Process.Start(verify)) { await Task.Run(() => p.WaitForExit()); if (p.ExitCode != 0) throw new InvalidOperationException("安装文件签名验证失败，已停止安装。"); }
            progress("请在 Windows 管理员提示中确认安装；取消后可以稍后重试。");
            try
            {
                var info = new ProcessStartInfo("msiexec.exe", "/i " + RcloneEngine.Quote(target) + " /passive /norestart") { UseShellExecute = true, Verb = "runas" };
                using (var p = Process.Start(info))
                {
                    await Task.Run(() => p.WaitForExit());
                    if (p.ExitCode == 3010) throw new InvalidOperationException("磁盘组件已安装，请重启电脑后再连接云盘。");
                    if (p.ExitCode != 0) throw new InvalidOperationException("磁盘组件未安装完成，请联系公司管理员安装 WinFsp。");
                }
            }
            catch (System.ComponentModel.Win32Exception) { throw new InvalidOperationException("安装需要 Windows 管理员确认。你可以稍后点击“安装磁盘组件”重试。"); }
            if (LocalSettings.WinFspFolder() == null) throw new InvalidOperationException("尚未检测到磁盘组件，请重启助手或联系管理员。");
        }
    }

    public sealed class MainForm : Form
    {
        readonly Color Ink = Color.FromArgb(73, 99, 140);
        readonly TextBox server = new TextBox(), username = new TextBox(), password = new TextBox(), cache = new TextBox();
        readonly ComboBox letter = new ComboBox();
        readonly CheckBox remember = new CheckBox(), auto = new CheckBox();
        readonly CheckBox keepPassword = new CheckBox(), autoMount = new CheckBox();
        readonly Button connect = new Button(), disconnect = new Button(), open = new Button(), driver = new Button(), encryptedLibraries = new Button();
        readonly Label status = new Label(), driverText = new Label();
        readonly NotifyIcon tray = new NotifyIcon();
        readonly RcloneEngine engine = new RcloneEngine();
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        Profile profile;
        CloudApi connectedApi;
        bool exiting, busy, polling, checkingChanges;
        readonly HttpClient changeClient = new HttpClient(CompanyCertificate.CreateHandler()) { Timeout = TimeSpan.FromSeconds(20) };
        string changeCursor = "";
        DateTime nextChangeCheck;
        readonly HashSet<string> changedDirectories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        int connectionGeneration;
        readonly FileChangePopup fileChanges = new FileChangePopup();
        async Task CheckChanges()
        {
            if (busy || !engine.Running || checkingChanges ||
                (DateTime.UtcNow < nextChangeCheck && changedDirectories.Count == 0)) return;
            int generation = connectionGeneration;
            checkingChanges = true;
            try
            {
                if (DateTime.UtcNow >= nextChangeCheck)
                {
                    nextChangeCheck = DateTime.UtcNow.AddSeconds(24);
                    var data = await CloudApi.FileChanges(changeClient, profile, changeCursor);
                    if (generation != connectionGeneration || busy || !engine.Running || exiting) return;
                    fileChanges.Present(data["items"] as IList);
                    changeCursor = Json.Text(data, "cursor");
                    if (Object.Equals(data["more"], true)) nextChangeCheck = DateTime.UtcNow;
                    object paths;
                    if (data.TryGetValue("refresh_dirs", out paths) && paths is IList)
                        foreach (string dir in ((IList)paths).OfType<string>())
                            if (changedDirectories.Count < 128) changedDirectories.Add(dir);
                }
                if (changedDirectories.Count > 0 && await engine.Pending() == 0 &&
                    generation == connectionGeneration && !busy && engine.Running && !exiting)
                {
                    string[] dirs = changedDirectories.ToArray();
                    await engine.ForgetDirectories(dirs);
                    if (generation == connectionGeneration) changedDirectories.ExceptWith(dirs);
                }
            }
            catch (HttpRequestException) { }
            catch (TaskCanceledException) { }
            catch (ArgumentException) { }
            catch (InvalidOperationException) { }
            catch (KeyNotFoundException) { }
            finally { checkingChanges = false; }
        }
        public MainForm(bool background)
        {
            Text = "苏盐新云盘 · 连接助手"; Font = new Font("Microsoft YaHei UI", 9F); BackColor = Color.FromArgb(245, 247, 250);
            ClientSize = new Size(590, 752); MinimumSize = new Size(606, 791); MaximumSize = new Size(800, 1000);
            StartPosition = FormStartPosition.CenterScreen; AutoScaleMode = AutoScaleMode.Dpi;
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application;
            var banner = new Panel { Dock = DockStyle.Top, Height = 117, BackColor = Color.FromArgb(52, 68, 91) };
            using (Stream logoStream = typeof(MainForm).Assembly.GetManifestResourceStream("CompanyDrive.Logo"))
            using (Image logo = Image.FromStream(logoStream))
            {
                banner.Controls.Add(new PictureBox { Image = new Bitmap(logo), SizeMode = PictureBoxSizeMode.Zoom,
                    Location = new Point(27, 28), Size = new Size(43, 50), BackColor = Color.White,
                    Padding = new Padding(4) });
            }
            banner.Controls.Add(new Label { Text = "苏盐新云盘", ForeColor = Color.White, Font = new Font(Font.FontFamily, 22F, FontStyle.Bold), AutoSize = true, Location = new Point(86, 22) });
            banner.Controls.Add(new Label { Text = "登录后，直接在资源管理器中访问团队文件。", ForeColor = Color.FromArgb(178, 191, 211), AutoSize = true, Location = new Point(88, 76) });
            Controls.Add(banner);
            var body = new Panel { Location = new Point(30, 136), Size = new Size(530, 601), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom };
            Controls.Add(body);
            var driverPanel = new Panel { Location = new Point(0, 0), Size = new Size(530, 64), BackColor = Color.FromArgb(235, 240, 247) };
            driverText.Location = new Point(14, 13); driverText.Size = new Size(338, 42); driverPanel.Controls.Add(driverText);
            StyleButton(driver, "安装磁盘组件", new Rectangle(369, 14, 146, 34), false); driver.Click += async (s, e) => await InstallDriver(); driverPanel.Controls.Add(driver); body.Controls.Add(driverPanel);
            AddField(body, "苏盐新云盘地址", server, 81); AddField(body, "账号", username, 151);
            AddField(body, "登录密码", password, 221); password.UseSystemPasswordChar = true;
            var driveLabel = new Label { Text = "电脑盘符", Location = new Point(0, 293), AutoSize = true }; body.Controls.Add(driveLabel);
            letter.Location = new Point(0, 316); letter.Size = new Size(110, 30); letter.DropDownStyle = ComboBoxStyle.DropDownList;
            for (char c = 'Z'; c >= 'D'; c--) letter.Items.Add(c.ToString()); body.Controls.Add(letter);
            remember.Text = "记住账号"; remember.Location = new Point(135, 316); remember.AutoSize = true; body.Controls.Add(remember);
            keepPassword.Text = "记住密码"; keepPassword.Location = new Point(240, 316); keepPassword.AutoSize = true; body.Controls.Add(keepPassword);
            auto.Text = "开机打开助手"; auto.Location = new Point(346, 316); auto.AutoSize = true; body.Controls.Add(auto);
            autoMount.Text = "开机时自动连接"; autoMount.Location = new Point(346, 338); autoMount.AutoSize = true; body.Controls.Add(autoMount);
            var cacheLabel = new Label { Text = "本机缓存位置", Location = new Point(0, 360), AutoSize = true }; body.Controls.Add(cacheLabel);
            cache.Location = new Point(0, 383); cache.Size = new Size(440, 28); body.Controls.Add(cache);
            var browse = new Button { Text = "选择…", Location = new Point(450, 381), Size = new Size(80, 31) };
            browse.Click += (s, e) => { using (var dialog = new FolderBrowserDialog { Description = "选择缓存位置，可使用 D/E 盘节省系统盘空间", SelectedPath = Directory.Exists(cache.Text) ? cache.Text : "" }) if (dialog.ShowDialog(this) == DialogResult.OK) cache.Text = dialog.SelectedPath; }; body.Controls.Add(browse);
            body.Controls.Add(new Label { Text = "打开文件时会下载到缓存，保存后自动上传。可选择空间较大的 D/E 盘。", ForeColor = Color.Gray, Location = new Point(0, 420), Size = new Size(530, 22), Font = new Font(Font.FontFamily, 8F) });
            StyleButton(connect, "登录并连接", new Rectangle(0, 454, 212, 43), true); connect.Click += async (s, e) => await Connect(); body.Controls.Add(connect);
            StyleButton(open, "打开云盘", new Rectangle(225, 454, 146, 43), false); open.Click += (s, e) => OpenDrive(); body.Controls.Add(open);
            StyleButton(disconnect, "断开", new Rectangle(384, 454, 146, 43), false); disconnect.Click += async (s, e) => await Disconnect(); body.Controls.Add(disconnect);
            StyleButton(encryptedLibraries, "加密资料库…", new Rectangle(0, 508, 160, 34), false);
            encryptedLibraries.Click += (s, e) => OpenEncryptedLibraries();
            body.Controls.Add(encryptedLibraries);
            status.Location = new Point(0, 552); status.Size = new Size(530, 49); status.ForeColor = Color.Gray; body.Controls.Add(status);
            profile = LocalSettings.Load(); if (String.IsNullOrEmpty(profile.Server)) profile.Server = LocalSettings.DefaultServer();
            server.Text = profile.Server; username.Text = profile.Username; cache.Text = String.IsNullOrEmpty(profile.Cache) ? Path.Combine(LocalSettings.Folder, "cache") : profile.Cache;
            remember.Checked = profile.Remember; auto.Checked = profile.AutoStart;
            keepPassword.Checked = profile.RememberPassword; autoMount.Checked = profile.AutoMount;
            remember.CheckedChanged += (s, e) => { if (!remember.Checked) keepPassword.Checked = false; };
            keepPassword.CheckedChanged += (s, e) => {
                if (keepPassword.Checked) remember.Checked = true;
                else { autoMount.Checked = false; ForgetSavedPassword(); }
                RefreshControls();
            };
            auto.CheckedChanged += (s, e) => {
                if (!auto.Checked) autoMount.Checked = false;
                try { LocalSettings.SetAutoStart(auto.Checked); profile.AutoStart = auto.Checked; SaveLoginPreferences(); }
                catch (Exception error) { SetStatus("开机设置保存失败：" + error.Message, true); }
                RefreshControls();
            };
            autoMount.CheckedChanged += (s, e) => {
                if (!autoMount.Checked || (profile.RememberPassword && MatchesSavedAccount())) {
                    profile.AutoMount = autoMount.Checked;
                    SaveLoginPreferences();
                }
            };
            server.TextChanged += (s, e) => ShowSignInHint();
            username.TextChanged += (s, e) => ShowSignInHint();
            letter.SelectedItem = letter.Items.Contains(profile.Letter) ? profile.Letter : "Z";
            tray.Icon = Icon; tray.Text = "苏盐新云盘"; tray.Visible = true;
            var menu = new ContextMenuStrip(); menu.Items.Add("打开苏盐新云盘", null, (s, e) => OpenDrive()); menu.Items.Add("显示连接助手", null, (s, e) => ShowMain());
            menu.Items.Add("加密资料库…", null, (s, e) => OpenEncryptedLibraries());
            menu.Items.Add("断开云盘", null, async (s, e) => await Disconnect());
            menu.Items.Add("退出", null, async (s, e) => await ExitApp()); tray.ContextMenuStrip = menu; tray.DoubleClick += (s, e) => ShowMain();
            FormClosing += (s, e) => { if (!exiting && busy) { e.Cancel = true; } else if (!exiting && engine.Running) { e.Cancel = true; Hide(); } else tray.Visible = false; };
            timer.Interval = 3000; timer.Tick += async (s, e) => await Poll();
            timer.Tick += async (s, e) => await CheckChanges(); timer.Start();
            FormClosed += (s, e) => { connectionGeneration++; fileChanges.Dispose(); changeClient.Dispose(); };
            RefreshDriver(); RefreshControls(); ShowSignInHint();
            Shown += async (s, e) => {
                if (background && profile.AutoMount && profile.RememberPassword) await Connect();
                else if (background && username.Text.Length > 0) password.Focus();
            };
        }
        void AddField(Control parent, string title, TextBox field, int top)
        { parent.Controls.Add(new Label { Text = title, Location = new Point(0, top), AutoSize = true }); field.Location = new Point(0, top + 24); field.Size = new Size(530, 29); parent.Controls.Add(field); }
        void StyleButton(Button button, string text, Rectangle bounds, bool primary)
        { button.Text = text; button.Bounds = bounds; button.FlatStyle = FlatStyle.Flat; button.FlatAppearance.BorderColor = Color.FromArgb(216, 224, 235); button.BackColor = primary ? Ink : Color.White; button.ForeColor = primary ? Color.White : Ink; button.Cursor = Cursors.Hand; }
        void SetStatus(string text, bool error) { status.Text = text; status.ForeColor = error ? Color.FromArgb(162, 68, 52) : Color.FromArgb(82, 105, 139); }
        void RefreshDriver()
        { bool ok = LocalSettings.WinFspFolder() != null; driverText.Text = ok ? "磁盘组件已安装\n可以连接为电脑盘。" : "首次使用需要安装磁盘组件\n安装时需要 Windows 管理员确认。"; driver.Enabled = !ok && !busy; driver.Text = ok ? "组件已就绪" : "安装磁盘组件"; }
        void RefreshControls()
        { connect.Enabled = !busy && !engine.Running; open.Enabled = engine.Running; encryptedLibraries.Enabled = !busy && engine.Running; disconnect.Enabled = !busy && engine.Running; server.Enabled = username.Enabled = password.Enabled = letter.Enabled = cache.Enabled = !busy && !engine.Running; remember.Enabled = keepPassword.Enabled = auto.Enabled = connect.Enabled; autoMount.Enabled = connect.Enabled && keepPassword.Checked && auto.Checked; RefreshDriver(); }

        void SaveLoginPreferences()
        {
            try { LocalSettings.Save(profile); }
            catch (Exception error) { SetStatus("本机登录设置保存失败：" + error.Message, true); }
        }
        bool MatchesSavedAccount()
        {
            try { return LocalSettings.NormalizeServer(server.Text) == profile.Server &&
                String.Equals(profile.Username, username.Text.Trim(), StringComparison.OrdinalIgnoreCase); }
            catch (InvalidOperationException) { return false; }
        }
        void ShowSignInHint()
        {
            if (!busy && !engine.Running)
                SetStatus(keepPassword.Checked && profile.RememberPassword && MatchesSavedAccount()
                    ? "已记住此账号，可直接点击“登录并连接”。" : "请输入网页登录密码。勾选“记住密码”后，下次可直接连接。", false);
        }
        void ForgetSavedPassword()
        {
            if (!profile.RememberPassword) return;
            profile.RememberPassword = false;
            profile.DevicePassword = "";
            profile.AutoMount = false;
            SetStatus("下次连接需要输入网页登录密码。", false);
            SaveLoginPreferences();
        }
        async Task InstallDriver()
        {
            if (busy) return; busy = true; RefreshControls();
            try { await DriverInstaller.Install(text => SetStatus(text, false)); SetStatus("磁盘组件已安装，现在可以登录并连接。", false); }
            catch (Exception e) { SetStatus(e.Message, true); }
            finally { busy = false; RefreshControls(); }
        }
        async Task Connect()
        {
            if (busy || engine.Running) return;
            busy = true; RefreshControls(); CloudApi api = null; string newToken = ""; Exception failure = null;
            try
            {
                await EndDeviceSession();
                if (LocalSettings.WinFspFolder() == null) throw new InvalidOperationException("请先点击上方“安装磁盘组件”，完成后再连接。");
                string address = LocalSettings.NormalizeServer(server.Text); string account = username.Text.Trim();
                if (String.IsNullOrEmpty(account)) throw new InvalidOperationException("请输入苏盐新云盘账号。");
                if (String.IsNullOrWhiteSpace(cache.Text)) throw new InvalidOperationException("请选择本机缓存位置。");
                var next = new Profile { Server = address, Username = account, Letter = Convert.ToString(letter.SelectedItem), Cache = Path.GetFullPath(cache.Text), Remember = remember.Checked, RememberPassword = keepPassword.Checked, AutoStart = auto.Checked, AutoMount = autoMount.Checked };
                bool sameAccount = MatchesSavedAccount();
                bool reuse = password.Text.Length == 0 && sameAccount && next.RememberPassword && profile.RememberPassword && !String.IsNullOrEmpty(profile.DevicePassword);
                if (!reuse && String.IsNullOrWhiteSpace(password.Text)) throw new InvalidOperationException("请输入本次连接的网页登录密码。");
                SetStatus("正在验证账号并连接云盘…", false);
                if (reuse)
                {
                    next.DevicePassword = profile.DevicePassword;
                    next.TokenId = profile.TokenId;
                }
                else
                {
                    api = new CloudApi(address);
                    await api.Login(account, password.Text);
                    password.Clear();
                    var device = await api.NewDevice();
                    next.DevicePassword = Json.Text(device, "password"); next.TokenId = newToken = Json.Text(device, "id");
                }
                try { await CloudApi.CheckDevice(next); }
                catch (DeviceAccessDenied) { if (reuse) keepPassword.Checked = false; throw; }
                await engine.Start(next);
                if (api != null && sameAccount && !String.IsNullOrEmpty(profile.TokenId) && profile.TokenId != next.TokenId)
                    try { await api.Revoke(profile.TokenId); } catch { }
                profile = next;
                connectedApi = api; api = null;
                connectionGeneration++; changedDirectories.Clear(); changeCursor = ""; nextChangeCheck = DateTime.MinValue;
                LocalSettings.Save(next);
                LocalSettings.SetAutoStart(next.AutoStart);
                SetStatus("已连接：苏盐新云盘（" + next.Letter + ":）。关闭窗口后仍会在右下角托盘运行。", false);
                tray.Text = "苏盐新云盘（" + next.Letter + ":）已连接";
                OpenDrive();
            }
            catch (Exception e)
            {
                failure = e;
            }
            if (failure != null)
            {
                password.Clear();
                if (api != null && !String.IsNullOrEmpty(newToken) && !engine.Running) try { await api.Revoke(newToken); } catch { }
                SetStatus(failure.Message, true);
            }
            if (api != null) api.Dispose(); busy = false; RefreshControls();
        }
        async Task Disconnect()
        {
            if (busy || !engine.Running) return; busy = true; RefreshControls();
            try { SetStatus("正在确认文件上传状态…", false); await engine.Stop(); await EndDeviceSession(); fileChanges.Dismiss(); SetStatus(profile.RememberPassword ? "已断开，此账号可直接重新连接。" : "已断开，重新连接时请输入密码。", false); tray.Text = "苏盐新云盘 · 未连接"; }
            catch (Exception e) { SetStatus(e.Message, true); ShowMain(); }
            finally { busy = false; RefreshControls(); }
        }
        async Task ExitApp()
        {
            if (busy) { ShowMain(); return; }
            if (engine.Running) { await Disconnect(); if (engine.Running) return; }
            await EndDeviceSession();
            exiting = true; tray.Visible = false; timer.Stop(); Close(); Application.Exit();
        }
        async Task EndDeviceSession()
        {
            var api = connectedApi;
            if (api == null) return;
            connectedApi = null;
            if (profile.RememberPassword) { api.Dispose(); return; }
            string token = profile.TokenId;
            profile.DevicePassword = "";
            connectionGeneration++;
            using (api)
            {
                try { await api.Revoke(token); } catch (InvalidOperationException) { }
            }
        }
        void OpenEncryptedLibraries()
        {
            if (busy || !engine.Running) return;
            ShowMain();
            using (var window = new EncryptedLibrariesForm(profile)) window.ShowDialog(this);
        }
        void OpenDrive()
        {
            if (!engine.Running || !Directory.Exists(engine.Letter + @":\")) { ShowMain(); return; }
            Process.Start(new ProcessStartInfo("explorer.exe", engine.Letter + @":\") { UseShellExecute = true });
        }
        void ShowMain() { Show(); WindowState = FormWindowState.Normal; Activate(); }
        async Task Poll()
        {
            if (busy || polling) return;
            polling = true;
            try
            {
                if (!engine.Running)
                {
                    await EndDeviceSession();
                    if (fileChanges.Visible) fileChanges.Dismiss();
                    if (await engine.FinishMountCleanup())
                    {
                        tray.Text = "苏盐新云盘 · 未连接";
                        SetStatus("云盘连接已结束，请重新连接。", true);
                    }
                    RefreshControls();
                    return;
                }
                int pending = await engine.Pending();
                SetStatus(pending > 0 ? "还有 " + pending + " 个文件正在上传或等待重试，请保持连接。" : "苏盐新云盘（" + engine.Letter + ":）已连接 · 当前没有待上传文件。", false);
            }
            catch { SetStatus("正在等待连接状态，暂时不要退出助手。", true); }
            finally { polling = false; }
        }
    }

    internal static class Program
    {
        [System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
        static extern int SetCurrentProcessExplicitAppUserModelID(string appID);
        [STAThread]
        static void Main(string[] args)
        {
            SetCurrentProcessExplicitAppUserModelID("CompanyDrive.Client");
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
            bool created;
            string sid = System.Security.Principal.WindowsIdentity.GetCurrent().User.Value;
            using (var mutex = new Mutex(true, @"Local\CompanyDriveClient-" + sid, out created))
            {
                if (!created) { MessageBox.Show("连接助手已在运行，请从 Windows 右下角托盘图标打开。", "苏盐新云盘"); return; }
                DriveMountCleanup.RemoveDisconnected();
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new MainForm(args.Contains("--background")));
            }
        }
    }
}
