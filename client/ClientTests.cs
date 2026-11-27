using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;

namespace CompanyDrive
{
    internal static class ClientTests
    {
        static int count;
        static void Check(bool condition, string description)
        { if (!condition) throw new Exception(description); count++; Console.WriteLine("PASS " + description); }
        static void Reject(Action action, string description)
        { try { action(); } catch (InvalidOperationException) { count++; Console.WriteLine("PASS " + description); return; } throw new Exception(description); }
        static void Main(string[] args)
        {
            Console.OutputEncoding = Encoding.UTF8;
            try { Run(args).GetAwaiter().GetResult(); }
            catch (Exception e) { Console.Error.WriteLine(e.ToString()); Environment.Exit(1); }
        }
        static async Task Run(string[] args)
        {
            LocalSettings.Folder = Path.GetFullPath(args[0]); Directory.CreateDirectory(LocalSettings.Folder);
            Check(LocalSettings.NormalizeServer("http://127.0.0.1:8765/dav/") == "http://127.0.0.1:8765", "normalize DAV endpoint");
            Check(LocalSettings.NormalizeServer("https://example.com/") == "https://example.com", "normalize HTTPS endpoint");
            Check(LocalSettings.NormalizeServer("http://192.168.1.20:8765") == "http://192.168.1.20:8765", "allow private LAN trial");
            Reject(() => LocalSettings.NormalizeServer("http://public.example.com"), "reject public plaintext endpoint");
            Reject(() => LocalSettings.NormalizeServer("https://user:pass@example.com"), "reject embedded credentials");
            Reject(() => LocalSettings.NormalizeServer("https://example.com/path?secret=x"), "reject unexpected paths and query");
            Reject(() => LocalSettings.NormalizeServer("file:///C:/private"), "reject non HTTP schemes");
            Check(RcloneEngine.Quote(@"C:\path with spaces\") == "\"C:\\path with spaces\\\\\"", "quote trailing Windows separator");
            Check(LocalSettings.CacheKey("https://a.example", "user") != LocalSettings.CacheKey("https://b.example", "user"), "isolate server caches");
            Check(LocalSettings.CacheKey("https://a.example", "one") != LocalSettings.CacheKey("https://a.example", "two"), "isolate account caches");
            var profile = new Profile { Server = "http://127.0.0.1:8765", Username = "demo", DevicePassword = "test-protected-secret", TokenId = "test" };
            LocalSettings.Save(profile);
            string settings = Path.Combine(LocalSettings.Folder, "profile.bin");
            Check(LocalSettings.Load().DevicePassword == "" && LocalSettings.Load().Username == "demo", "remember account without device password");
            var legacy = new { profile.Server, profile.Username, profile.DevicePassword, profile.TokenId, AutoConnect = true };
            File.WriteAllBytes(settings, ProtectedData.Protect(Encoding.UTF8.GetBytes(Json.Write(legacy)), LocalSettings.Entropy, DataProtectionScope.CurrentUser));
            var migrated = LocalSettings.Load();
            Check(migrated.AutoStart && migrated.DevicePassword == "" && migrated.TokenId == "test", "upgrade keeps launch preference and discards device password");
            var saved = Json.Read(Encoding.UTF8.GetString(ProtectedData.Unprotect(File.ReadAllBytes(settings), LocalSettings.Entropy, DataProtectionScope.CurrentUser)));
            Check(!saved.ContainsKey("DevicePassword") && !saved.ContainsKey("AutoConnect"), "old credentials removed from encrypted settings");
            profile.RememberPassword = true;
            LocalSettings.Save(profile);
            Check(LocalSettings.Load().RememberPassword && LocalSettings.Load().DevicePassword == profile.DevicePassword, "explicit opt-in saves scoped device credential");
            profile.RememberPassword = false;
            LocalSettings.Save(profile);
            Check(!LocalSettings.Load().RememberPassword && LocalSettings.Load().DevicePassword == "", "forgetting removes stored credential");
            profile.Remember = false;
            LocalSettings.Save(profile);
            Check(LocalSettings.Load().Username == "" && LocalSettings.Load().TokenId == "", "unchecked remember clears saved account");
            using (var api = new CloudApi(profile.Server))
            {
                var status = await api.Call("GET", "/api/status"); Check(status.ContainsKey("initialized"), "live server protocol");
                string name = await api.Login("demo", "Preview-Only-2026!"); Check(name.Length > 0, "normal account sign-in");
                var token = await api.NewDevice(); profile.DevicePassword = Json.Text(token, "password"); profile.TokenId = Json.Text(token, "id");
                Check(profile.DevicePassword.Length > 30, "automatic per-device credential");
                try
                {
                    await CloudApi.CheckDevice(profile); Check(true, "WebDAV device authentication");
                    string exe = args[1];
                    var info = await RcloneEngine.Configure(profile, exe, "lsf company: --max-depth 1");
                    Check(!info.Arguments.Contains(profile.DevicePassword), "no credential in process arguments");
                    Check(!File.ReadAllText(Path.Combine(LocalSettings.Folder, "empty.conf")).Contains(profile.DevicePassword), "no credential in rclone config file");
                    using (var p = Process.Start(info))
                    {
                        string output = await p.StandardOutput.ReadToEndAsync(); string error = await p.StandardError.ReadToEndAsync();
                        await Task.Run(() => p.WaitForExit());
                        Check(p.ExitCode == 0 && output.Contains("公司公共资料"), "real rclone lists server groups");
                    }
                }
                finally { api.Revoke(profile.TokenId).GetAwaiter().GetResult(); }
                bool revoked = false; try { await CloudApi.CheckDevice(profile); } catch (InvalidOperationException) { revoked = true; }
                Check(revoked, "revoked device cannot reconnect");
            }
            using (var api = new CloudApi(profile.Server))
            {
                bool rejected = false; try { await api.Login("demo", "wrong-password"); } catch (InvalidOperationException) { rejected = true; }
                Check(rejected, "wrong account password is rejected");
            }
            Console.WriteLine(count + " client checks passed.");
        }
    }
}
