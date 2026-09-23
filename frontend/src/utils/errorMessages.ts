export type FriendlyError = {
  title: string;
  message: string;
  actions: string[];
  technical: string;
};

function cleanRawError(error: unknown): string {
  return String(error || '').trim();
}

export function humanizeExecutionError(error: unknown): FriendlyError {
  const technical = cleanRawError(error);
  const lower = technical.toLowerCase();

  if (
    lower.includes('sql server') ||
    lower.includes('sqldriverconnect') ||
    lower.includes('odbc driver') ||
    lower.includes('08001')
  ) {
    if (
      lower.includes('encryption not supported') ||
      lower.includes('ssl provider') ||
      lower.includes('trustservercertificate') ||
      lower.includes('invalid connection string attribute')
    ) {
      return {
        title: 'SQL Server connection failed',
        message: 'F-Pulse could not connect to the SQL Server destination. The driver reported an encryption or certificate setting problem.',
        actions: [
          'Open the SQL Server connection and try Trust Server Certificate / Encrypt settings.',
          'Confirm the server name, instance name, port, database, username, and password.',
          'Make sure SQL Server accepts remote TCP/IP connections and the firewall allows the port.',
        ],
        technical,
      };
    }

    return {
      title: 'SQL Server connection failed',
      message: 'F-Pulse could not reach the SQL Server destination.',
      actions: [
        'Confirm the server name, instance name, port, and database.',
        'Check that SQL Server is running and TCP/IP remote connections are enabled.',
        'Verify the credentials and firewall rules, then test the connection again.',
      ],
      technical,
    };
  }

  if (lower.includes('connection refused') || lower.includes('server is not found') || lower.includes('not accessible')) {
    return {
      title: 'Connection failed',
      message: 'F-Pulse could not reach the target system.',
      actions: [
        'Check the host, port, and network access from this machine.',
        'Confirm the service is running and accepts remote connections.',
        'Test the saved connection before running the pipeline again.',
      ],
      technical,
    };
  }

  return {
    title: 'Step failed',
    message: technical || 'This step failed while running.',
    actions: [],
    technical,
  };
}
