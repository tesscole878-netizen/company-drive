using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.CSharp.RuntimeBinder;
using Microsoft.Win32;

namespace CompanyDrive
{
    internal static class DriveMountCleanup
    {
        public const string VolumeName = "苏盐新云盘";
        const string MountHost = "suyan-drive-";
        const string MountShare = @"\" + VolumeName;
        const string ExplorerMounts = @"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2";
        const int ConnectionUnavailable = 1201;
        const int NotConnected = 2250;
        const uint UpdateProfile = 1;
        const uint RemoteDrive = 4;
        const int DriveRemoved = 0x0080;
        const int DriveAdded = 0x0100;
        static readonly HashSet<string> removedLetters = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        const int DirectoryUpdated = 0x1000;
        const uint PathUnicode = 0x0005;
        const uint FlushNotifications = 0x1000;
        const string ThisPc = "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}";

        // Include the known older labels so an upgrade can remove their Explorer entries.
        static readonly string[] Drives = {
            VolumeName, "公司云盘", "company{gF-2g}", "company{FJM7V}", "Suyan HTTPS verification"
        };

        public static string MountName(string letter)
        {
            return @"\\" + MountHost + letter.ToLowerInvariant() + MountShare;
        }

        static string MountLetter(string remote)
        {
            string prefix = @"\\" + MountHost;
            if (!remote.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
                || !remote.EndsWith(MountShare, StringComparison.OrdinalIgnoreCase)
                || remote.Length != prefix.Length + 1 + MountShare.Length)
                return null;
            char letter = Char.ToUpperInvariant(remote[prefix.Length]);
            return letter >= 'D' && letter <= 'Z' ? letter + ":" : null;
        }

        public static void RemoveDisconnected(string finishedLetter = null)
        {
            try
            {
                if (GetLogicalDrives() == 0) return;
                var staleLetters = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                bool changed = RemoveRememberedConnections(staleLetters);
                staleLetters.UnionWith(ExplorerStaleLetters());
                if (!String.IsNullOrEmpty(finishedLetter))
                    staleLetters.Add(finishedLetter + ":");
                changed |= RemoveExplorerEntries(staleLetters);
                lock (removedLetters)
                {
                    removedLetters.RemoveWhere(local => !IsUnmapped(local));
                    foreach (string local in staleLetters)
                        if (IsUnmapped(local)) removedLetters.Add(local);
                    staleLetters.UnionWith(removedLetters);
                }
                if (changed && staleLetters.Count == 0) RefreshThisPc();
                foreach (string local in staleLetters)
                {
                    if (IsUnmapped(local))
                    {
                        // A full refresh of This PC can reinsert Explorer's cached copy
                        // of the drive. Notify the open view about this drive only.
                        SHChangeNotifyPath(DriveRemoved, PathUnicode | FlushNotifications, local + "\\", IntPtr.Zero);
                    }
                }
            }
            // Registry access can be restricted by workstation policy.
            catch (IOException error) { Trace.TraceWarning(error.Message); }
            catch (UnauthorizedAccessException error) { Trace.TraceWarning(error.Message); }
            catch (System.Security.SecurityException error) { Trace.TraceWarning(error.Message); }
        }

        public static void RefreshAfterConnect(string letter)
        {
            string local = letter + ":", remote;
            if (GetConnection(local, out remote) != 0 ||
                !String.Equals(remote, MountName(letter), StringComparison.OrdinalIgnoreCase)) return;
            SHChangeNotifyPath(DriveAdded, PathUnicode | FlushNotifications, local + "\\", IntPtr.Zero);
            RemoveDisconnected();
        }

        static HashSet<string> ExplorerStaleLetters()
        {
            var letters = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            // MountPoints2 is a label cache. An already open This PC can keep displaying
            // removed drives even after its directory is refreshed.
            Type shellType = Type.GetTypeFromProgID("Shell.Application");
            if (shellType == null) return letters;
            try
            {
                dynamic shell = Activator.CreateInstance(shellType);
                dynamic computer = shell.NameSpace(17); // The shell namespace also exists without an open window.
                if (computer != null) CollectCompanyLetters(computer, letters, false);
                foreach (dynamic window in shell.Windows())
                {
                    try
                    {
                        dynamic folder = window.Document.Folder;
                        if (!String.Equals((string)folder.Self.Path, ThisPc, StringComparison.OrdinalIgnoreCase))
                            continue;
                        CollectCompanyLetters(folder, letters, true);
                    }
                    catch (COMException) { } // The window may have closed.
                    catch (RuntimeBinderException) { } // Some shell windows are not folder views.
                }
            }
            catch (COMException error) { Trace.TraceWarning(error.Message); }
            catch (RuntimeBinderException error) { Trace.TraceWarning(error.Message); }
            return letters;
        }

        static void CollectCompanyLetters(dynamic folder, HashSet<string> letters, bool openView)
        {
            var stale = new Dictionary<string, object>();
            foreach (dynamic item in folder.Items())
            {
                string path = (string)item.Path;
                string local = path == null ? "" : path.TrimEnd('\\');
                if (!IsDriveLetter(local) || !IsCompanyDriveLabel((string)item.Name, local)) continue;
                letters.Add(local);
                if (openView) stale[local] = item;
            }
            // A removed drive path no longer resolves to the item held by an open Explorer window.
            // Send that window's actual PIDL so Explorer can match the cached item.
            foreach (var entry in stale)
            {
                IntPtr item;
                if (!IsUnmapped(entry.Key) || SHGetIDListFromObject(entry.Value, out item) != 0) continue;
                try { SHChangeNotify(DriveRemoved, FlushNotifications, item, IntPtr.Zero); }
                finally { Marshal.FreeCoTaskMem(item); }
            }
        }

        static bool IsCompanyDriveLabel(string title, string local)
        {
            if (String.IsNullOrEmpty(title)) return false;
            int end = title.LastIndexOf(") (", StringComparison.Ordinal);
            int start = title.LastIndexOf(@" (\\", StringComparison.Ordinal);
            if (start < 0 || end <= start || !title.EndsWith(") (" + local + ")", StringComparison.OrdinalIgnoreCase))
                return false;
            return IsCompanyMount(title.Substring(start + 2, end - start - 2) + @"\" + title.Substring(0, start));
        }

        static bool IsDriveLetter(string local)
        {
            return local.Length == 2 && local[0] >= 'A' && local[0] <= 'Z' && local[1] == ':';
        }

        static bool IsUnmapped(string local)
        {
            if (!IsDriveLetter(local)) return false;
            uint mapped = GetLogicalDrives();
            if (mapped == 0 || (mapped & (1u << (local[0] - 'A'))) != 0 || HasLocalDevice(local))
                return false;
            string remote;
            if (GetConnection(local, out remote) != NotConnected) return false;
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Network\" + local[0]))
                return key == null;
        }

        static bool RemoveRememberedConnections(HashSet<string> staleLetters)
        {
            string provider = WinFspProviderName();
            if (String.IsNullOrEmpty(provider)) return false;
            bool changed = false;
            for (char letter = 'A'; letter <= 'Z'; letter++)
            {
                string local = letter + ":";
                using (RegistryKey remembered = Registry.CurrentUser.OpenSubKey(@"Network\" + letter))
                {
                    if (remembered == null || !IsWinFsp(remembered.GetValue("ProviderName") as string, provider))
                        continue;
                    string remote = remembered.GetValue("RemotePath") as string;
                    if (!IsCompanyMount(remote)) continue;
                    string ignored;
                    int status = GetConnection(local, out ignored);
                    if (status != ConnectionUnavailable && status != NotConnected) continue;
                    // Only forget an inactive network connection. Never force an unmount.
                    if (HasLocalDevice(local)) continue;
                    int result = WNetCancelConnection2(local, UpdateProfile, false);
                    if (result == NotConnected && GetConnection(local, out ignored) == NotConnected && !HasLocalDevice(local))
                    {
                        // Some provider versions leave the profile key behind after the device is already gone.
                        using (RegistryKey network = Registry.CurrentUser.OpenSubKey("Network", true))
                        using (RegistryKey orphan = network == null ? null : network.OpenSubKey(letter.ToString()))
                        {
                            if (orphan == null || orphan.SubKeyCount != 0 ||
                                !IsWinFsp(orphan.GetValue("ProviderName") as string, provider) ||
                                !String.Equals(orphan.GetValue("RemotePath") as string, remote, StringComparison.OrdinalIgnoreCase))
                                continue;
                            network.DeleteSubKey(letter.ToString(), false);
                        }
                    }
                    else if (result != 0) continue;
                    if (IsUnmapped(local))
                    {
                        staleLetters.Add(local);
                        changed = true;
                    }
                }
            }
            return changed;
        }

        static bool RemoveExplorerEntries(HashSet<string> staleLetters)
        {
            uint mapped = GetLogicalDrives();
            if (mapped == 0) return false;
            var live = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            bool allIdentified = true;
            for (char letter = 'A'; letter <= 'Z'; letter++)
            {
                if ((mapped & (1u << (letter - 'A'))) == 0) continue;
                string local = letter + ":";
                if (GetDriveType(local + "\\") != RemoteDrive) continue;
                string remote;
                if (GetConnection(local, out remote) != 0 || String.IsNullOrEmpty(remote))
                {
                    allIdentified = false;
                    continue;
                }
                live.Add(remote.TrimEnd('\\'));
            }

            // A failed disconnect can still be present in the profile. Preserve its label.
            using (RegistryKey root = Registry.CurrentUser.OpenSubKey("Network"))
            {
                if (root != null)
                    foreach (string name in root.GetSubKeyNames())
                        using (RegistryKey entry = root.OpenSubKey(name))
                        {
                            string remote = entry == null ? null : entry.GetValue("RemotePath") as string;
                            if (remote != null) live.Add(remote.TrimEnd('\\'));
                        }
            }

            bool changed = false;
            using (RegistryKey mounts = Registry.CurrentUser.OpenSubKey(ExplorerMounts, true))
            {
                if (mounts == null) return false;
                foreach (string key in mounts.GetSubKeyNames())
                {
                    string remote = key.Replace('#', '\\');
                    if (!IsCompanyMount(remote) || live.Contains(remote)) continue;
                    string local = MountLetter(remote);
                    if (local != null)
                    {
                        if (!IsUnmapped(local)) continue;
                        staleLetters.Add(local);
                    }
                    if (local == null && !allIdentified) continue;
                    using (RegistryKey entry = mounts.OpenSubKey(key))
                    {
                        if (entry == null || (local == null && entry.SubKeyCount != 0)) continue;
                    }
                    if (local != null && IsUnmapped(local)) mounts.DeleteSubKeyTree(key, false);
                    else if (local == null) mounts.DeleteSubKey(key, false);
                    else continue;
                    changed = true;
                }
            }
            return changed;
        }

        static bool IsCompanyMount(string remote)
        {
            if (String.IsNullOrEmpty(remote)) return false;
            remote = remote.TrimEnd('\\');
            foreach (string name in Drives)
                if (String.Equals(remote, @"\\server\" + name, StringComparison.OrdinalIgnoreCase)) return true;
            return MountLetter(remote) != null;
        }

        static string WinFspProviderName()
        {
            using (RegistryKey key = Registry.LocalMachine.OpenSubKey(
                @"SYSTEM\CurrentControlSet\Services\WinFsp.Np\NetworkProvider"))
                return key == null ? null : key.GetValue("Name") as string;
        }

        static bool IsWinFsp(string name, string provider)
        {
            if (String.IsNullOrEmpty(name)) return false;
            name = name.Split('\0')[0].Trim();
            return String.Equals(name, "WinFsp.Np", StringComparison.OrdinalIgnoreCase)
                || String.Equals(name, provider.Split('\0')[0].Trim(), StringComparison.OrdinalIgnoreCase);
        }

        static bool HasLocalDevice(string local)
        {
            var target = new StringBuilder(1024);
            if (QueryDosDevice(local, target, target.Capacity) != 0) return true;
            return Marshal.GetLastWin32Error() != 2; // Only a definite absence is safe to clean.
        }

        static int GetConnection(string local, out string remote)
        {
            uint capacity = 1024;
            var buffer = new StringBuilder((int)capacity);
            int status = WNetGetConnection(local, buffer, ref capacity);
            remote = buffer.ToString();
            return status;
        }

        static void RefreshThisPc()
        {
            IntPtr item;
            if (SHGetSpecialFolderLocation(IntPtr.Zero, 0x11, out item) != 0) return; // CSIDL_DRIVES
            try { SHChangeNotify(DirectoryUpdated, FlushNotifications, item, IntPtr.Zero); }
            finally { Marshal.FreeCoTaskMem(item); }
        }

        [DllImport("kernel32.dll")]
        static extern uint GetLogicalDrives();

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern uint GetDriveType(string root);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint QueryDosDevice(string name, StringBuilder target, int capacity);

        [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
        static extern int WNetGetConnection(string local, StringBuilder remote, ref uint capacity);

        [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
        static extern int WNetCancelConnection2(string name, uint flags, [MarshalAs(UnmanagedType.Bool)] bool force);

        [DllImport("shell32.dll")]
        static extern int SHGetSpecialFolderLocation(IntPtr owner, int folder, out IntPtr item);

        [DllImport("shell32.dll")]
        static extern int SHGetIDListFromObject([MarshalAs(UnmanagedType.IUnknown)] object obj, out IntPtr item);

        [DllImport("shell32.dll")]
        static extern void SHChangeNotify(int eventId, uint flags, IntPtr first, IntPtr second);

        [DllImport("shell32.dll", EntryPoint = "SHChangeNotify", CharSet = CharSet.Unicode, ExactSpelling = true)]
        static extern void SHChangeNotifyPath(int eventId, uint flags, string path, IntPtr second);
    }
}
