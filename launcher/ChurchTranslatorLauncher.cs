using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace ChurchTranslatorLauncher
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string script = Path.Combine(root, "scripts", "bootstrap.ps1");
            if (!File.Exists(script))
            {
                MessageBox.Show("Could not find scripts\\bootstrap.ps1 in the application folder.", "Church Sermon Translator Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            var outputBuilder = new StringBuilder();
            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + Quote(script) + " -FromLauncher",
                WorkingDirectory = root,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            try
            {
                using (var process = new Process { StartInfo = psi })
                {
                    process.OutputDataReceived += (sender, args) =>
                    {
                        if (args.Data != null)
                        {
                            lock (outputBuilder) { outputBuilder.AppendLine(args.Data); }
                        }
                    };
                    process.ErrorDataReceived += (sender, args) =>
                    {
                        if (args.Data != null)
                        {
                            lock (outputBuilder) { outputBuilder.AppendLine(args.Data); }
                        }
                    };

                    process.Start();
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    process.WaitForExit();

                    if (process.ExitCode != 0)
                    {
                        string log = outputBuilder.ToString();
                        MessageBox.Show("App startup failed (Exit code " + process.ExitCode + "):\n\n" + (string.IsNullOrWhiteSpace(log) ? "Unknown error" : log), "Church Sermon Translator", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    }
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not launch app: " + ex.Message, "Church Sermon Translator Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }
    }
}
