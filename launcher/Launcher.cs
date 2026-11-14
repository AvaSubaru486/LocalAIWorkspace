using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "runtime", "python", "python.exe");
        if (!File.Exists(python))
        {
            MessageBox.Show("缺少内置 Python。请完整解压安装包，不要只复制 EXE。", "Local AI Workspace", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        try
        {
            Directory.CreateDirectory(Path.Combine(root, "data", "logs"));
            ProcessStartInfo start = new ProcessStartInfo(python, "-I -X utf8 -u -m app.launcher");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.StandardOutputEncoding = System.Text.Encoding.UTF8;
            start.StandardErrorEncoding = System.Text.Encoding.UTF8;
            string logPath = Path.Combine(root, "data", "logs", "launcher-" + Process.GetCurrentProcess().Id + ".log");
            using (StreamWriter log = new StreamWriter(logPath, true, System.Text.Encoding.UTF8))
            using (Process process = new Process())
            {
                log.AutoFlush = true;
                process.StartInfo = start;
                DataReceivedEventHandler write = delegate(object sender, DataReceivedEventArgs args) { if (args.Data != null) lock (log) log.WriteLine(args.Data); };
                process.OutputDataReceived += write;
                process.ErrorDataReceived += write;
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                if (process.ExitCode != 0)
                    MessageBox.Show("启动失败。请查看 " + logPath + "，或运行 Start-LocalAI.cmd 查看详情。", "Local AI Workspace", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Local AI Workspace", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
