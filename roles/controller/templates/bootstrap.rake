{% macro lb_cluster() -%}
  {% for host in groups['nodes'] -%}
    "{{ hostvars[host].consul_listen_ip }}"
  {%- if not loop.last -%},{%- endif -%}
{%- endfor -%}
{% endmacro -%}
{% macro lb_internal_cluster() -%}
  {% for host in groups['nodes'] -%}
    "{{ hostvars[host].etcd_listen_ip }}"
  {%- if not loop.last -%},{%- endif -%}
{%- endfor -%}
{% endmacro -%}
task bootstrap: :environment do

  location = Location.find_by(name: "{{ region_name }}")
  location = Location.create!(name: "{{ region_name }}") if location.nil?
  region = Region.find_by name: "{{ availability_zone_name }}", location: location
  region = Region.create!(name: "{{ availability_zone_name }}", location: location) if region.nil?
  if region.nil?
    puts "Error, region not available."
    return false
  end

  Rake::Task['install'].execute

  puts "Setting General Settings..."
  {% if license_key != "" %}
  puts "...License Key"
  Setting.find_by(name: 'license_key')&.update value: "{{ license_key }}"
  {% endif %}

  puts "...Hostname"
  Setting.find_by(name: 'hostname')&.update value: "{{ cs_portal_domain }}"

  puts "Container Registry Settings..."
  puts "...LetsEncrypt"
  Setting.find_by(name: 'cr_le')&.update value: "{{ cs_portal_domain }}"
  puts "...Base URL"
  Setting.find_by(name: 'registry_base_url')&.update value: "{{ cs_registry_domain }}"
  puts "...Node"
  Setting.find_by(name: 'registry_node')&.update value: "{{ registry_server_ip }}"
  puts "...SSH Port"
  Setting.find_by(name: 'registry_ssh_port')&.update value: "{{ registry_ssh_port }}"

  puts "LetsEncrypt..."
  Setting.find_by(name: 'le_validation_server')&.update value: "{{ node_ip }}:3000"

  puts "Creating Metric Client"
  mc = MetricClient.find_by(endpoint: "{{ prometheus_endpoint }}")
  mc = MetricClient.create!(
    endpoint: "{{ prometheus_endpoint }}",
    username: "{{ prometheus_username }}",
    password: "{{ prometheus_password }}"
  ) if mc.nil?

  # Log Client is used by the controller
  # `loki_*` settings on the region are used by containers.
  lc = LogClient.find_by(endpoint: "{{ loki_controller_endpoint }}")
  lc = LogClient.create!(
    endpoint: "{{ loki_controller_endpoint }}",
    username: "{{ loki_username }}",
    password: "{{ loki_password }}"
  ) if lc.nil?
  

  if mc.nil? || lc.nil?
    raise "Missing Log or Metric Client"
  end

  region.update metric_client: mc,
                log_client: lc,
                loki_endpoint: "{{ loki_node_endpoint }}"
  

  puts "Creating Nodes..."

  {% for host in groups['nodes'] -%}
  puts "...creating node {{ hostvars[host]['ansible_hostname'] }}..."
  Node.create!(
    label: '{{ hostvars[host]['ansible_hostname'] }}',
    hostname: '{{ hostvars[host]['ansible_hostname'] }}',
    primary_ip: '{{ hostvars[host].consul_listen_ip }}',
    public_ip: '{{ hostvars[host].ansible_default_ipv4.address }}',
    region: region,
    active: true,    
    ssh_port: {{ hostvars[host].ssh_port | int }}
  ) unless Node.where(primary_ip: '{{ hostvars[host].consul_listen_ip }}').exists?
  {{ '' }}
  {{ '' }}
  {%- endfor -%}
  {{ '' }}
  puts "Configuring Network..."
  network = Network.find_by(cidr: '{{ calico_network }}')
  if network
    network.regions << region unless network.regions.include?(region)
  else
    network = Network.create!(
      cidr: '{{ calico_network }}',
      is_public: false,
      is_ipv4: true,
      active: true,
      name: '{{ calico_network_name | lower }}',
      label: '{{ availability_zone_name }} Network'
    )
    network.regions << region
  end

  {% if cs_admin_create -%}
  puts "Creating default admin account"
  group = UserGroup.find_by(is_default: true)
  if group.nil?
    default_billing_plan = BillingPlan.find_by(is_default: true)
    group = UserGroup.create!(name: 'default', is_default: true, billing_plan: default_billing_plan)
    region = Region.first
    group.regions << region if region
  end
  unless User.where(email: '{{ cs_admin_email }}').exists?
    user = User.new(
      fname: 'Admin',
      lname: 'Admin',
      email: '{{ cs_admin_email }}',
      bypass_billing: true,
      currency: ENV['CURRENCY'],
      is_admin: true,
      password: '{{ cs_admin_password }}',
      password_confirmation: '{{ cs_admin_password }}',
      user_group: group
    )
    user.skip_confirmation!
    user.save
  end

  {%- endif -%}  
{{ '' }}
{% if floating_ip == '0.0.0.0' or not enable_floating_ip %}
floating_ip = "{{ hostvars[groups['nodes'][0]].ansible_default_ipv4.address|default(ansible_all_ipv4_addresses[0]) }}"
{% else %}
floating_ip = "{{ floating_ip }}"
{% endif %} 
{{ '' }}
  lb = LoadBalancer.find_by public_ip: floating_ip
  if lb.nil?
    lb = LoadBalancer.create!(
      label: "{{ lb_name }}",
      region: region,
      domain: "{{ cs_app_url }}",
      ext_ip: [ {{ lb_cluster() }} ],
      internal_ip: [ {{ lb_internal_cluster() }} ],
      public_ip: floating_ip,
      shared_certificate: "{{ lb_shared_cert }}",
      direct_connect: {% if calico_network_ipip %}false{% else %}true{% endif %}
    )
  else
    lb.update ext_ip: [ {{ lb_cluster() }} ], internal_ip: [ {{ lb_internal_cluster() }} ]
  end

  unless Dns::Zone.where(name: "{{ cs_app_zone }}").exists?
    begin
      Dns::Zone.create!(name: "{{ cs_app_zone }}")
    rescue => e
      ExceptionAlertService.new(e, 'fb85bd8aefed7b69').perform
    end
  end

  existing_installation = Feature.where(name: 'updated_cr_cert').exists?
  Feature.setup!
  Feature.find_by(name: 'updated_cr_cert').update(active: true) unless existing_installation

  Setting.registry_selinux # Load and create setting
{% if selinux %}
  Setting.find_by(name: 'registry_selinux', category: 'container_registry').update value: true
{% else %}
  Setting.find_by(name: 'registry_selinux', category: 'container_registry').update value: false
{% endif %}

  puts "Done."

end
{{ '' }}
