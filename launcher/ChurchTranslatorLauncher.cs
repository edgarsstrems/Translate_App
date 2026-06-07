using System;
using System.Diagnostics;
using System.Drawing;
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
            Application.Run(new LauncherForm());
        }
    }

    internal sealed class LauncherForm : Form
    {
        private readonly TextBox logBox = new TextBox();
        private readonly Button startButton = new Button();
        private readonly Button closeButton = new Button();
        private Process process;

        public LauncherForm()
        {
            Text = "Church Sermon Translator";
            Width = 760;
            Height = 520;
            MinimumSize = new Size(640, 420);
            StartPosition = FormStartPosition.CenterScreen;

            string iconPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "assets", "app.ico");
            if (File.Exists(iconPath))
            {
                Icon = new Icon(iconPath);
            }

            var title = new Label
            {
                Text = "Church Sermon Translator",
                Dock = DockStyle.Top,
                Height = 44,
                Font = new Font(Font.FontFamily, 16, FontStyle.Bold),
                TextAlign = ContentAlignment.MiddleLeft,
                Padding = new Padding(12, 0, 0, 0)
            };

            var subtitle = new Label
            {
                Text = "Checking setup and starting the app...",
                Dock = DockStyle.Top,
                Height = 28,
                Padding = new Padding(12, 0, 0, 0)
            };

            logBox.Dock = DockStyle.Fill;
            logBox.Multiline = true;
            logBox.ReadOnly = true;
            logBox.ScrollBars = ScrollBars.Vertical;
            logBox.Font = new Font("Consolas", 9);
            logBox.BackColor = Color.FromArgb(250, 250, 250);

            startButton.Text = "Start";
            startButton.Width = 110;
            startButton.Click += (sender, args) => StartBootstrap();

            closeButton.Text = "Close";
            closeButton.Width = 110;
            closeButton.Click += (sender, args) => Close();

            var buttons = new FlowLayoutPanel
            {
                Dock = DockStyle.Bottom,
                Height = 48,
                FlowDirection = FlowDirection.RightToLeft,
                Padding = new Padding(8)
            };
            buttons.Controls.Add(closeButton);
            buttons.Controls.Add(startButton);

            Controls.Add(logBox);
            Controls.Add(buttons);
            Controls.Add(subtitle);
            Controls.Add(title);

            Shown += (sender, args) => StartBootstrap();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (process != null && !process.HasExited)
            {
                var result = MessageBox.Show(
                    "Setup or the app is still running. Close it now?",
                    "Church Sermon Translator",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Question);
                if (result != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
            }

            base.OnFormClosing(e);
        }

        private void StartBootstrap()
        {
            if (process != null && !process.HasExited)
            {
                return;
            }

            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string script = Path.Combine(root, "scripts", "bootstrap.ps1");
            if (!File.Exists(script))
            {
                AppendLog("Could not find scripts\\bootstrap.ps1 beside the launcher.");
                return;
            }

            startButton.Enabled = false;
            logBox.Clear();
            AppendLog("Starting setup from: " + root);

            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + Quote(script) + " -FromLauncher",
                WorkingDirectory = root,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            process.OutputDataReceived += (sender, args) => { if (args.Data != null) AppendLog(args.Data); };
            process.ErrorDataReceived += (sender, args) => { if (args.Data != null) AppendLog(args.Data); };
            process.Exited += (sender, args) =>
            {
                BeginInvoke(new Action(() =>
                {
                    int code = process.ExitCode;
                    AppendLog("");
                    AppendLog(code == 0 ? "App closed." : "Startup failed. Exit code: " + code);
                    startButton.Enabled = true;
                }));
            };

            try
            {
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                AppendLog("Could not start PowerShell: " + ex.Message);
                startButton.Enabled = true;
            }
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private void AppendLog(string message)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action<string>(AppendLog), message);
                return;
            }

            logBox.AppendText(message + Environment.NewLine);
        }
    }
}
