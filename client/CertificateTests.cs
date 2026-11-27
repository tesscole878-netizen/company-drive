using System;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;

namespace CompanyDrive
{
    internal static class CertificateTests
    {
        static void Check(bool condition, string message)
        {
            if (!condition) throw new Exception(message);
            Console.WriteLine("PASS " + message);
        }

        static int Main(string[] args)
        {
            try
            {
                ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
                if (args[0] == "fixtures")
                {
                    using (var valid = new X509Certificate2(Path.Combine(args[1], "valid.cer")))
                    using (var expired = new X509Certificate2(Path.Combine(args[1], "expired.cer")))
                    using (var other = new X509Certificate2(Path.Combine(args[1], "other.cer")))
                    using (var clientOnly = new X509Certificate2(Path.Combine(args[1], "client-only.cer")))
                    {
                        Check(CompanyCertificate.Validate(null, valid, null, SslPolicyErrors.RemoteCertificateChainErrors), "company chain accepted");
                        Check(!CompanyCertificate.Validate(null, valid, null, SslPolicyErrors.RemoteCertificateChainErrors | SslPolicyErrors.RemoteCertificateNameMismatch), "hostname mismatch rejected");
                        Check(!CompanyCertificate.Validate(null, expired, null, SslPolicyErrors.RemoteCertificateChainErrors), "expired certificate rejected");
                        Check(!CompanyCertificate.Validate(null, other, null, SslPolicyErrors.RemoteCertificateChainErrors), "unrelated certificate authority rejected");
                        Check(!CompanyCertificate.Validate(null, clientOnly, null, SslPolicyErrors.RemoteCertificateChainErrors), "client-only certificate rejected");
                        Check(!CompanyCertificate.Validate(null, null, null, SslPolicyErrors.RemoteCertificateNotAvailable), "missing certificate rejected");
                    }
                }
                else
                {
                    using (var api = new CloudApi("https://192.168.80.252:8443"))
                    {
                        var health = api.Call("GET", "/health").GetAwaiter().GetResult();
                        Check(health.ContainsKey("ok") && Convert.ToBoolean(health["ok"]), "live HTTPS using bundled company certificate");
                    }
                }
                return 0;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine(error.GetType().Name + ": " + error.Message);
                return 1;
            }
        }
    }
}
