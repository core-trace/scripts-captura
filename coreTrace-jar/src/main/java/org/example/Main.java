package org.example;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Random;

public class Main {

    public static void main(String[] args) throws InterruptedException {
        DateTimeFormatter dtf = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");
        Random random = new Random();

        System.out.println("========================================================================");
        System.out.println("   ____   ___   ____  _____  _____ ____    _    ____ _____ ");
        System.out.println("  / ___| / _ \\ |  _ \\| ____||_   _|  _ \\  / \\  / ___| ____|");
        System.out.println(" | |    | | | || |_) ||  _|    | | | |_) |/ _ \\| |   |  _|  ");
        System.out.println(" | |___ | |_| ||  _ <| |___   | | |  _ </ ___ \\ |___| |___ ");
        System.out.println("  \\____| \\___/ |_| \\_\\_____|  |_| |_| \\_\\/   \\_\\____|_____|");
        System.out.println("                        CORETRACE v1.0                         ");
        System.out.println("========================================================================");

        System.out.println("[" + LocalDateTime.now().format(dtf) + "] [INFO] [System] Inicializando CoreTrace Agent...");
        Thread.sleep(1000);
        System.out.println("[" + LocalDateTime.now().format(dtf) + "] [INFO] [Config] Servidor detectado: AWS-EC2-CoreTrace-S1");
        System.out.println("[" + LocalDateTime.now().format(dtf) + "] [INFO] [Status] Aguardando ciclo de captura de métricas...\n");

        int simulacoes = 0;
        while (simulacoes < 10) {
            simulacoes++;
            String timestamp = LocalDateTime.now().format(dtf);


            double cpu = 60.0 + (random.nextDouble() * 75.0);
            double ram = 30.0 + (random.nextDouble() * 40.0);
            double disco = 45.2;

            System.out.println("[" + timestamp + "] [INFO] [MetricCollector] --- Captura #" + simulacoes + " ---");
            System.out.printf("[%s] [INFO] [CPU] Uso Global: %.2f%%\n", timestamp, cpu);
            System.out.printf("[%s] [INFO] [RAM] Uso da Memória: %.2f%%\n", timestamp, ram);
            System.out.printf("[%s] [INFO] [DISCO] Uso do Disco: %.2f%%\n", timestamp, disco);

            // Simulação de Alerta 
            if (cpu > 80.0) {
                System.out.println("[" + timestamp + "] [WARN] [ThresholdAlert] ATENÇÃO: Uso crítico de CPU (" + String.format("%.2f", cpu) + "%)!");
                System.out.println("[" + timestamp + "] [INFO] [SlackIntegration] Disparando notificação para o canal #alerta-infra-ifood...");
                System.out.println("[" + timestamp + "] [INFO] [JiraIntegration] Simulando criação de ticket no Jira...");
            }

            System.out.println("[" + timestamp + "] [INFO] [Database] [LOCAL] Métricas gravadas temporariamente na base local.\n");

            Thread.sleep(3000);
        }

    }
}